"""Computed text-contrast evidence for populated browser journeys."""
import json

# Solid sRGB computed values only. Unknown rendering gets an explicit review record.
CONTRAST=r'''() => {
 const rgb=s=>{const m=s.match(/^rgba?\(([^)]+)\)$/);if(!m)return null;
   const v=m[1].split(/[, /]+/).filter(Boolean).map(Number);return [v[0],v[1],v[2],v[3]??1];};
 const over=(a,b)=>a.slice(0,3).map((v,i)=>v*a[3]+b[i]*(1-a[3]));
 const lum=c=>c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
 const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
 if(Math.abs(ratio([0,0,0],[255,255,255])-21)>1e-8 || ratio([12,12,12],[12,12,12])!==1)throw Error('contrast control failed');
 const records=[],unknown=[],disabled=[];
 function check(el,text,pseudo=null){
   if(!text.trim() || !el.checkVisibility({checkVisibilityCSS:true,checkOpacity:true}))return;
   const box=el.getBoundingClientRect();if(!box.width || !box.height || el.closest('.visually-hidden'))return;
   const label=(el.id?'#'+el.id:el.tagName.toLowerCase()+(el.className?'.'+String(el.className).trim().replaceAll(' ','.'):''));
   if(el.closest(':disabled,[aria-disabled="true"]')){disabled.push({label,text:text.slice(0,120)});return;}
   const chain=[];for(let p=el;p;p=p.parentElement)chain.unshift(p);
   let bg=[255,255,255];
   for(const p of chain){const s=getComputedStyle(p),color=rgb(s.backgroundColor);
     if(s.backgroundImage!=='none' || Number(s.opacity)!==1 || !color){unknown.push({label,text:text.slice(0,120),reason:'background image, opacity or unsupported color'});return;}
     bg=over(color,bg);
   }
   const style=getComputedStyle(el,pseudo),fg=rgb(style.color);if(!fg){unknown.push({label,text,reason:'unsupported foreground'});return;}
   const foreground=over(fg,bg),size=parseFloat(style.fontSize),weight=parseInt(style.fontWeight)||400;
   const minimum=size>=24 || (size>=18.6666666667 && weight>=700)?3:4.5;
   records.push({label,pseudo,text:text.slice(0,160),foreground,background:bg,font_size:size,font_weight:weight,minimum,ratio:ratio(foreground,bg)});
 }
 const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
 for(let n;n=walker.nextNode();){if(['SCRIPT','STYLE','OPTION'].includes(n.parentElement.tagName))continue;
   const range=document.createRange();range.selectNodeContents(n);if(![...range.getClientRects()].some(r=>r.width&&r.height))continue;
   check(n.parentElement,n.textContent);}
 for(const el of document.querySelectorAll('input,textarea'))check(el,el.value||el.placeholder,el.value?null:'::placeholder');
 return {checked:records.length,failed:records.filter(r=>r.ratio<r.minimum),unknown,disabled,
         minimum:records.length?Math.min(...records.map(r=>r.ratio)):null};
}'''


class VisualChecks:
    def __init__(self, page, path):
        self.page, self.path, self.records = page, path, []

    def check(self, name):
        record = {'name':name, 'theme':self.page.evaluate('async()=>(await import("/app.js")).effectiveTheme()'),
                  'width':self.page.viewport_size['width'], **self.page.evaluate(CONTRAST)}
        self.records.append(record)
        self.path.write_text(json.dumps(self.records,indent=2)+'\n')

    def themes(self, keys, name, directory, widths=(1280,1440), target=None):
        for width in widths:
            self.page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                if target is not None:
                    target.scroll_into_view_if_needed()  # Capture setup; task actions still use keys.
                self.check(name)
                assert self.page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                self.page.screenshot(path=str(directory/(name+'-'+theme+'-'+str(width)+'.png')))

    def assert_clean(self):
        findings = [r for r in self.records if r['failed'] or r['unknown']]
        assert not findings, [(r['name'],r['theme'],r['width'],r['failed'],r['unknown']) for r in findings]
        print('RICH_CONTRAST_OK',len(self.records),'states',sum(r['checked'] for r in self.records),'text checks',flush=True)
