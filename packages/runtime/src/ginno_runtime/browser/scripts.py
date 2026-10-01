"""Page-side bridge scripts injected into every tab (both tracks).

Clean-room ports of the two script families the QwenWork extension proved out
(accessibility-tree.js + page-bridge.js): a ref-based accessibility snapshot
(``__ginnoAT``) and semantic page operations (``__ginnoBridge``). They are
plain IIFEs — injected by CDP ``Page.addScriptToEvaluateOnNewDocument`` on the
B 轨 and shipped as content scripts in the extension (identical source), so
tool code never has to care which track is active.
"""

from __future__ import annotations

ACCESSIBILITY_TREE_JS = r"""
(function () {
  if (globalThis.__ginnoAT) return;
  var refs = new Map();          // ref -> WeakRef(element)
  var byEl = new WeakMap();      // element -> ref
  var next = 1;

  function roleOf(el) {
    var r = el.getAttribute && el.getAttribute("role");
    if (r) return r;
    var t = el.tagName.toLowerCase();
    var map = { a: "link", button: "button", select: "combobox", textarea: "textbox",
      ul: "list", ol: "list", li: "listitem", table: "table", tr: "row",
      form: "form", nav: "navigation", main: "main", article: "article",
      header: "header", footer: "footer", img: "image", h1: "heading",
      h2: "heading", h3: "heading", h4: "heading", h5: "heading", h6: "heading" };
    if (map[t]) return map[t];
    if (t === "input") {
      var it = (el.type || "text").toLowerCase();
      if (["text", "email", "password", "tel", "url"].includes(it)) return "textbox";
      if (it === "search") return "searchbox";
      if (["checkbox", "radio", "range", "number", "file", "submit", "reset"].includes(it))
        return { checkbox: "checkbox", radio: "radio", range: "slider",
                 number: "spinbutton", file: "button", submit: "button", reset: "button" }[it];
      return "textbox";
    }
    if (t === "td" || t === "th") return "cell";
    if (el.onclick || el.onmousedown || el.onmouseup) return "button";
    return "";
  }

  var INTERACTIVE = ["button", "link", "textbox", "searchbox", "combobox", "checkbox",
    "radio", "slider", "spinbutton", "menuitem", "menuitemcheckbox", "menuitemradio",
    "option", "tab", "switch"];

  function labelOf(el) {
    var s = function (x) { return (x || "").trim().slice(0, 100); };
    var al = el.getAttribute && el.getAttribute("aria-label");
    if (al) return s(al);
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) { var t = document.getElementById(lb); if (t) return s(t.textContent); }
    var ti = el.getAttribute && el.getAttribute("title");
    if (ti) return s(ti);
    if (el.placeholder) return s(el.placeholder);
    var tag = el.tagName;
    if (tag === "BUTTON" || tag === "A" ||
        ["H1","H2","H3","H4","H5","H6"].includes(tag) ||
        (tag === "LABEL" && el.control)) return s(el.textContent);
    if (tag === "IMG") return s(el.alt);
    var role = el.getAttribute && el.getAttribute("role");
    if (role && ["heading","listitem","article","status","alert","tooltip"].includes(role))
      return s(el.textContent);
    return "";
  }

  function visible(el) {
    var cs = window.getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden" &&
      cs.opacity !== "0" && el.offsetWidth > 0 && el.offsetHeight > 0;
  }

  function refFor(el) {
    var prev = byEl.get(el);
    if (prev) {
      var w = refs.get(prev);
      if (w && w.deref() === el) return prev;
    }
    var r = "ref_" + (next++);
    refs.set(r, new WeakRef(el));
    byEl.set(el, r);
    return r;
  }

  // Snapshot: indented tree lines `[ref_N] role "label" (state...)`.
  // mode "interactive" walks all depths; "all" respects depth.
  function generate(opts) {
    opts = opts || {};
    var mode = opts.mode || "all";
    var depth = Math.min(opts.depth || 15, 50);
    var maxChars = Math.min(opts.max_chars || 50000, 200000);
    var rootRef = opts.ref_id || null;
    var lines = [], size = 0, truncated = false, lastRef = null;

    var root = null;
    if (rootRef) {
      var w = refs.get(rootRef);
      root = w ? w.deref() : null;
      if (!root)
        return { error: "Element not found: " + rootRef +
          ". Use browser_read_page or browser_find to get a fresh ref." };
    }
    function walk(el, d) {
      if (truncated) return;
      if (mode === "all" && d > depth) return;
      if (!visible(el)) return;
      var role = roleOf(el);
      var isHit = mode === "all" || INTERACTIVE.includes(role) || role !== "";
      if (isHit) {
        var label = labelOf(el);
        var ref = refFor(el);
        var line = "  ".repeat(Math.min(d, 10)) + "[" + ref + "] " + role;
        if (label) line += ' "' + label + '"';
        var states = [];
        if (el.disabled) states.push("disabled");
        if (el.checked !== undefined && (el.type || "") !== "radio")
          states.push(el.checked ? "checked" : "unchecked");
        if (el.readOnly) states.push("readonly");
        if (el.required) states.push("required");
        if (el.tagName === "SELECT") states.push("options=" + el.options.length);
        if (states.length) line += " (" + states.join(", ") + ")";
        if (size + line.length > maxChars) {
          truncated = true;
          lines.push("");
          lines.push("[TRUNCATED: output limit reached at " + lastRef +
            ". Some elements are not shown.]");
          lines.push('[To see more: use ref_id="' + lastRef +
            '" to focus on a subtree, or increase max_chars beyond ' + maxChars + ".]");
          return;
        }
        lines.push(line);
        size += line.length + 1;
        lastRef = ref;
      }
      for (var i = 0; i < el.children.length; i++) walk(el.children[i], d + 1);
    }
    walk(root || document.body, 0);
    var text = (truncated
      ? "[Warning: output was truncated — not all elements are shown. " +
        "Use ref_id to focus on a subtree or increase max_chars.]\n"
      : "") + lines.join("\n");
    return { tree: text, truncated: truncated };
  }

  function coords(ref, scroll) {
    var w = refs.get(ref);
    var el = w ? w.deref() : null;
    if (!el) return null;
    if (scroll) el.scrollIntoView({ behavior: "instant", block: "center", inline: "nearest" });
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2,
             width: r.width, height: r.height };
  }

  function elInfo(ref) {
    var w = refs.get(ref);
    var el = w ? w.deref() : null;
    if (!el) return { found: false };
    return {
      found: true,
      tagName: el.tagName,
      type: (el.getAttribute && el.getAttribute("type")) || "",
      name: el.getAttribute && (el.getAttribute("name") || ""),
      id: el.id || "",
      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    };
  }

  function markRef(ref, attr) {
    // Tag the element so CDP DOM.querySelector can find it for
    // DOM.setFileInputFiles (file upload, design §4.5).
    var w = refs.get(ref);
    var el = w ? w.deref() : null;
    if (!el) return false;
    el.setAttribute(attr, "1");
    return true;
  }

  function clearMark(ref, attr) {
    var w = refs.get(ref);
    var el = w ? w.deref() : null;
    if (el) el.removeAttribute(attr);
  }

  globalThis.__ginnoAT = {
    generate: generate, coords: coords, elInfo: elInfo,
    markRef: markRef, clearMark: clearMark, refFor: refFor,
    resolve: function (ref) {
      var w = refs.get(ref);
      return w ? w.deref() : null;
    },
  };
})();
"""

PAGE_BRIDGE_JS = r"""
(function () {
  if (globalThis.__ginnoBridge) return;
  var AT = function () { return globalThis.__ginnoAT; };

  // Semantic form fill with framework-compatible events: native value setter
  // + InputEvent (React controlled components), execCommand for contentEditable.
  function fill(ref, value) {
    var at = AT(); if (!at) return { success: false, error: "bridge unavailable" };
    var w = null;
    // resolve element via at internals: reuse coords infra by walking refs is
    // not exposed; bridge relies on at.elInfo + direct ref resolution below.
    var el = at.resolve ? at.resolve(ref) : null;
    if (!el) return { success: false, error: "Element not found: " + ref +
      ". Use browser_read_page or browser_find to get a fresh ref." };
    try {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      var tag = el.tagName.toLowerCase();
      if (tag !== "input" && tag !== "select" && tag !== "textarea" &&
          !el.isContentEditable)
        return { success: false, error: "ref 指向 " + tag +
          " 元素,不是表单控件。请用 browser_read_page/browser_find 找到 input/select/textarea 的 ref。" };
      if (tag === "select") {
        var ok = false;
        for (var i = 0; i < el.options.length; i++) {
          var o = el.options[i];
          if (String(o.value) === String(value) || o.text === String(value)) {
            el.value = o.value; ok = true; break;
          }
        }
        if (!ok) return { success: false, error: "No matching option found" };
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (tag === "input") {
        var ty = (el.type || "").toLowerCase();
        if (ty === "checkbox" || ty === "radio") {
          el.checked = !!value;
          el.dispatchEvent(new Event("change", { bubbles: true }));
        } else {
          if (ty === "file")
            return { success: false, error: "File uploads require browser_file_upload" };
          setNativeValue(el, String(value));
        }
      } else if (tag === "textarea") {
        setNativeValue(el, String(value));
      } else if (el.isContentEditable) {
        el.focus();
        document.execCommand("selectAll", false, null);
        document.execCommand("insertText", false, String(value));
      } else {
        setNativeValue(el, String(value));
      }
      if ((tag === "textarea" || (tag === "input" &&
           ["text","password","search","tel","url"].includes((el.type||"").toLowerCase())))
          && el.setSelectionRange) {
        var len = (el.value || "").length;
        el.setSelectionRange(len, len);
      }
      return { success: true, fieldName: el.name || el.id || ref };
    } catch (e) {
      return { success: false, error: (e && e.message) || "fill failed" };
    }
  }

  function setNativeValue(el, value) {
    var proto = el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    try {
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true, inputType: "insertText", data: value }));
    } catch (e) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Keyword scoring search (design §4.2): label 3 / role 2 / text 1.
  function find(query, max) {
    var at = AT(); if (!at) return { results: [], error: "bridge unavailable" };
    max = max || 20;
    var terms = String(query).toLowerCase().split(/\s+/).filter(Boolean);
    var hits = [];
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    var node;
    while ((node = walker.nextNode())) {
      var el = node;
      var role = (el.getAttribute && (el.getAttribute("role") ||
        el.tagName.toLowerCase())) || "";
      var label = (el.getAttribute && (el.getAttribute("aria-label") ||
        el.getAttribute("title"))) || "";
      var text = ((el.textContent || "").trim().slice(0, 100)) || "";
      var score = 0;
      var lo = (label + " " + role + " " + text).toLowerCase();
      for (var i = 0; i < terms.length; i++) {
        if (label.toLowerCase().includes(terms[i])) score += 3;
        if (role.toLowerCase().includes(terms[i])) score += 2;
        if (text.toLowerCase().includes(terms[i])) score += 1;
      }
      if (score > 0 && lo) {
        var ref = at.refFor(el);
        hits.push({ ref: ref, text: label || text || role, role: role, score: score });
      }
    }
    hits.sort(function (a, b) { return b.score - a.score; });
    return { results: hits.slice(0, max) };
  }

  // Article-priority text extraction (design §4.4).
  function pageText(maxChars) {
    maxChars = maxChars || 50000;
    var candidates = ["article", "main", '[class*="article-body"]',
      '[class*="articleBody"]', '[class*="post-content"]',
      '[class*="entry-content"]', '[class*="content-body"]',
      '[role="main"]', ".content", "#content"];
    var best = null, bestLen = 0;
    for (var i = 0; i < candidates.length; i++) {
      var els = document.querySelectorAll(candidates[i]);
      for (var j = 0; j < els.length; j++) {
        var len = ((els[j].textContent || "").length) || 0;
        if (len > bestLen) { bestLen = len; best = els[j]; }
      }
    }
    var root = best || document.body;
    var text = ((root && root.textContent) || "")
      .replace(/\s+/g, " ").trim();
    if (text.length > maxChars) text = text.slice(0, maxChars) + "... (truncated)";
    return { title: document.title, url: window.location.href, content: text,
             sourceElement: root ? root.tagName.toLowerCase() : "body" };
  }

  function click(ref) {
    var at = AT(); if (!at) return { success: false, error: "bridge unavailable" };
    var c = at.coords(ref, true);
    if (!c) return { success: false, error: "Element not found: " + ref };
    return { success: true, x: c.x, y: c.y };
  }

  globalThis.__ginnoBridge = {
    fill: fill, find: find, pageText: pageText, click: click,
  };
})();"""

BRIDGE_SOURCES = [ACCESSIBILITY_TREE_JS, PAGE_BRIDGE_JS]
