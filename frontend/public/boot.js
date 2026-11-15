// 最初の描画の前に、言語とテーマを <html> に当てる（React より先に、同期で読む）。
// 1. アプリ本体が設定ファイルの好みを <html data-prefs="…"> に書いて渡す。あれば、ブラウザの保存へ写す
//    （ブラウザの保存はポートごとに分かれる。ポートが変わっても、次の起動で同じ好みで開く。D-050）
// 2. 覚えている値が無ければ、ブラウザの言語と OS の明暗の設定から決める（画面仕様 10.3）。
(function () {
  var root = document.documentElement;
  var theme = null;
  var lang = null;
  var prefs = null;
  try {
    prefs = JSON.parse(root.getAttribute("data-prefs") || "null");
  } catch (e) {
    prefs = null;
  }
  try {
    if (prefs && typeof prefs === "object") {
      for (var name in prefs) {
        if (Object.prototype.hasOwnProperty.call(prefs, name) && typeof prefs[name] === "string") {
          window.localStorage.setItem("circuit-sim." + name, prefs[name]);
        }
      }
    }
    theme = window.localStorage.getItem("circuit-sim.theme");
    lang = window.localStorage.getItem("circuit-sim.lang");
  } catch (e) {
    /* 保存領域を使えない設定のブラウザでも、設定ファイルの値か既定で開く */
    if (prefs && typeof prefs === "object") {
      theme = prefs.theme || null;
      lang = prefs.lang || null;
    }
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
