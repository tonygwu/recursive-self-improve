"""Native keyboard traversal for browser acceptance; never moves DOM focus directly."""
import json

from playwright.sync_api import expect


class KeyboardActions:
    def __init__(self, page, evidence):
        self.page = page
        self.evidence = evidence
        self.steps = []

    def focused(self, target):
        expect(target).to_be_focused()
        info = target.evaluate('''el => {
            const s=getComputedStyle(el),r=el.getBoundingClientRect();
            const parent=el.closest('.search'),p=parent&&getComputedStyle(parent);
            return {tag:el.tagName,id:el.id,text:el.textContent.slice(0,100),
                outline:s.outlineStyle,width:s.outlineWidth,
                searchFocus:parent&&parent.matches(':focus-within') ?
                    {id:parent.id,border:p.borderColor,shadow:p.boxShadow}:null,
                visible:r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth};
        }''')
        assert info['visible'], info
        outlined = info['outline'] != 'none' and float(info['width'].removesuffix('px')) > 0
        # Rules search draws its inspected focus indicator on the enclosing label.
        search = info['searchFocus']
        assert outlined or (search and search['id'] == 'rules-search' and search['shadow'] != 'none'), info
        return info

    def reach(self, target):
        expect(target).to_be_visible()
        expect(target).to_be_enabled()
        trail = []
        for _ in range(250):
            if target.evaluate('el => el === document.activeElement'):
                info = self.focused(target)
                self.steps.append({'action': 'reach', 'tabs': len(trail), 'trail': trail, 'focus': info})
                self.save()
                return
            self.page.keyboard.press('Tab')
            trail.append(self.page.evaluate('''() => {
                const e=document.activeElement;
                return {tag:e.tagName,id:e.id,text:e.textContent.slice(0,100)};
            }'''))
        self.steps.append({'action': 'unreachable', 'target': str(target), 'trail': trail})
        self.save()
        raise AssertionError('Tab cannot reach ' + str(target))

    def activate(self, target, key='Enter'):
        self.reach(target)
        self.page.keyboard.press(key)
        self.steps.append({'action': 'activate', 'key': key, 'target': str(target)})
        self.save()

    def text(self, target, value):
        self.reach(target)
        self.page.keyboard.press('ControlOrMeta+a')
        self.page.keyboard.type(value)
        expect(target).to_have_value(value)
        self.steps.append({'action': 'type', 'target': str(target), 'characters': len(value)})
        self.save()

    def choose(self, target, value):
        self.reach(target)
        options = target.locator('option').evaluate_all('items => items.map(el => ({value:el.value,label:el.textContent}))')
        label = next(item['label'] for item in options if item['value'] == value)
        # Native type-ahead works in Chromium's macOS headless select as well as
        # the regular control. Its popup arrow keys do not commit there.
        self.page.keyboard.type(label)
        self.page.keyboard.press('Tab')
        expect(target).to_have_value(value)
        self.steps.append({'action': 'select', 'target': str(target), 'value': value})
        self.save()

    def theme(self, theme):
        current = self.page.evaluate('async() => (await import("/app.js")).effectiveTheme()')
        if current != theme:
            self.activate(self.page.get_by_role('button', name=theme.title() + ' theme', exact=True))
        assert self.page.evaluate('async() => (await import("/app.js")).effectiveTheme()') == theme

    def save(self):
        self.evidence.write_text(json.dumps(self.steps, indent=2) + '\n')
