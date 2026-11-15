// 画面の中で、見えている文字の欠け・はみ出しと、文字のコントラスト、ヘッダーの重なりを測る
// （tests/test_app_browser.py が使う。AC-1-1、AC-10-2、AC-10-4）
() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const v = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:v[0], g:v[1], b:v[2], a: v.length > 3 ? v[3] : 1}; };
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const bgOf = (el) => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
    }
    let out = {r: 255, g: 255, b: 255};
    for (const c of layers.reverse()) out = {r: c.r * c.a + out.r * (1 - c.a), g: c.g * c.a + out.g * (1 - c.a), b: c.b * c.a + out.b * (1 - c.a)};
    return out;
  };
  const low = [], cut = [];
  for (const el of document.querySelectorAll("body *")) {
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.visibility === "hidden" || rect.width === 0 || rect.height === 0 || el.closest("[hidden]")) continue;
    if (el.closest(".visually-hidden")) continue;
    if (el.closest('[aria-disabled="true"]')) continue;   // 使えない操作の文字は基準の外（design-system 2.3）
    const fg = parse(style.color), bg = bgOf(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    if (ratio < 4.5) low.push(`${el.className || el.tagName}: ${el.textContent.trim().slice(0, 30)} ${ratio.toFixed(2)}`);
    if (el.scrollWidth > el.clientWidth + 1 && style.overflow !== "visible")
      cut.push(`${el.className || el.tagName}: ${el.textContent.trim().slice(0, 30)}`);
    if (rect.right > innerWidth + 0.5 || rect.left < -0.5) cut.push(`画面の外: ${el.textContent.trim().slice(0, 30)}`);
  }
  const header = document.querySelector("[data-testid=header]").getBoundingClientRect();
  const parts = [...document.querySelectorAll("[data-testid=header] > *")].filter((e) => e.getBoundingClientRect().width > 0)
    .map((e) => { const r = e.getBoundingClientRect(); return {name: e.dataset.testid || e.className, l: r.left, r: r.right, t: r.top, b: r.bottom}; });
  const overlaps = [];
  for (let i = 1; i < parts.length; i++) if (parts[i].l < parts[i - 1].r - 0.5) overlaps.push(`${parts[i - 1].name} / ${parts[i].name}`);
  const outside = parts.filter((p) => p.t < header.top - 0.5 || p.b > header.bottom + 0.5 || p.r > header.right + 0.5).map((p) => p.name);
  return {low, cut, overlaps, outside, headerHeight: header.height};
}
