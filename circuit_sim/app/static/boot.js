// 最初の描画の前に、言語とテーマを <html> に当てる（React より先に、同期で読む）。
// 覚えている値が無ければ、ブラウザの言語と OS の明暗の設定から決める（画面仕様 10.3）。
(function () {
  var root = document.documentElement;
  var theme = null;
  var lang = null;
  try {
    theme = window.localStorage.getItem("circuit-sim.theme");
    lang = window.localStorage.getItem("circuit-sim.lang");
  } catch (e) {
    /* 保存領域を使えない設定のブラウザでも、既定で開く */
  }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  if (lang !== "ja" && lang !== "en") {
    var nav = (navigator.languages && navigator.languages[0]) || navigator.language || "";
    lang = nav.toLowerCase().indexOf("ja") === 0 ? "ja" : "en";
  }
  root.setAttribute("data-theme", theme);
  root.style.colorScheme = theme;
  root.setAttribute("lang", lang);
})();
