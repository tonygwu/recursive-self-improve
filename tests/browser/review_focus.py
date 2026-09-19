"""One observation of the current Review card's focus and viewport geometry."""


def review_card_snapshot(page):
    # A locator's resolved handle can detach before its evaluation when a
    # preview replaces the subtree. Read identity, focus and geometry together.
    # This is a single observation, with no waiting or retrying an assertion.
    return page.evaluate('''() => {
        const card = document.querySelector('.review-card--selected');
        const rect = card?.getBoundingClientRect();
        return {id: card?.dataset.learningId ?? null,
            focused: Boolean(card && card === document.activeElement),
            visible: Boolean(rect && rect.bottom > 0 && rect.top < innerHeight),
            connected: Boolean(card?.isConnected),
            top: rect?.top ?? null, bottom: rect?.bottom ?? null};
    }''')


def assert_review_card_focus(page):
    observed = review_card_snapshot(page)
    assert observed['connected'] and observed['focused'] and observed['visible'], observed
    return observed
