"""Surface adapter: the only code that knows what a browser is.

Exposes two verbs -- `perceive()` returns a pruned accessibility-tree
snapshot plus a screenshot, `act(action)` executes one action -- and both
discovery and replay drive the world exclusively through them.

Why isolate it: this is the seam that swaps for a legacy-web or desktop
adapter later (design notes §4). A desktop adapter would trade Playwright
for an OS accessibility API but expose the *same* role+name locator shape,
so artifacts recorded against this adapter survive the swap. That is only
true if nothing outside this package ever touches Playwright directly.
"""
