/* LifeContext landing page: theme toggle, install tabs, copy buttons.
   Loaded in <head> so a saved theme applies before first paint.
   No network access, no cookies; the only storage is the theme choice. */
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "lifecontext-theme";

  root.classList.add("js");

  function readTheme() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  function saveTheme(value) {
    try {
      window.localStorage.setItem(KEY, value);
    } catch (e) {
      /* Private mode or blocked storage: the choice lasts for this page only. */
    }
  }

  var saved = readTheme();
  if (saved === "light" || saved === "dark") {
    root.setAttribute("data-theme", saved);
  }

  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function activeTheme() {
    var pinned = root.getAttribute("data-theme");
    if (pinned === "light" || pinned === "dark") {
      return pinned;
    }
    return darkQuery && darkQuery.matches ? "dark" : "light";
  }

  function setupTheme() {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) {
      return;
    }
    function sync() {
      button.setAttribute("aria-pressed", activeTheme() === "dark" ? "true" : "false");
    }
    button.hidden = false;
    sync();
    button.addEventListener("click", function () {
      var next = activeTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      saveTheme(next);
      sync();
    });
    if (darkQuery && darkQuery.addEventListener) {
      darkQuery.addEventListener("change", sync);
    }
  }

  function setupTabs() {
    var boxes = document.querySelectorAll("[data-tabs]");
    Array.prototype.forEach.call(boxes, function (box) {
      var list = box.querySelector("[role='tablist']");
      var tabs = Array.prototype.slice.call(box.querySelectorAll("[role='tab']"));
      var panels = tabs.map(function (tab) {
        return document.getElementById(tab.getAttribute("aria-controls"));
      });
      if (!list || !tabs.length) {
        return;
      }

      function select(index, moveFocus) {
        tabs.forEach(function (tab, i) {
          var on = i === index;
          tab.setAttribute("aria-selected", on ? "true" : "false");
          tab.tabIndex = on ? 0 : -1;
          if (panels[i]) {
            panels[i].hidden = !on;
          }
        });
        if (moveFocus) {
          tabs[index].focus();
        }
      }

      tabs.forEach(function (tab, i) {
        tab.addEventListener("click", function () {
          select(i, false);
        });
        tab.addEventListener("keydown", function (event) {
          var last = tabs.length - 1;
          var target = null;
          if (event.key === "ArrowRight") {
            target = i === last ? 0 : i + 1;
          } else if (event.key === "ArrowLeft") {
            target = i === 0 ? last : i - 1;
          } else if (event.key === "Home") {
            target = 0;
          } else if (event.key === "End") {
            target = last;
          }
          if (target !== null) {
            event.preventDefault();
            select(target, true);
          }
        });
      });

      list.hidden = false;
      select(0, false);
    });
  }

  function setupCopy() {
    var status = document.getElementById("copy-status");
    var buttons = document.querySelectorAll("[data-copy]");

    function announce(text) {
      if (status) {
        status.textContent = "";
        window.setTimeout(function () {
          status.textContent = text;
        }, 30);
      }
    }

    function selectText(node) {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }

    Array.prototype.forEach.call(buttons, function (button) {
      var source = document.getElementById(button.getAttribute("data-copy"));
      var label = button.querySelector("[data-label]");
      if (!source || !label) {
        return;
      }
      var idle = label.textContent;
      var timer = null;

      function show(state, text) {
        button.setAttribute("data-state", state);
        label.textContent = text;
        announce(text);
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          button.removeAttribute("data-state");
          label.textContent = idle;
        }, 2000);
      }

      function fallback() {
        selectText(source);
        var ok = false;
        try {
          ok = document.execCommand("copy");
        } catch (e) {
          ok = false;
        }
        show(ok ? "done" : "manual", ok ? button.getAttribute("data-done") : button.getAttribute("data-manual"));
      }

      button.hidden = false;
      button.addEventListener("click", function () {
        var text = source.textContent.replace(/\s+$/, "") + "\n";
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(function () {
            show("done", button.getAttribute("data-done"));
          }, fallback);
        } else {
          fallback();
        }
      });
    });
  }

  function setupMenu() {
    var menus = document.querySelectorAll("details.menu");
    Array.prototype.forEach.call(menus, function (menu) {
      var summary = menu.querySelector("summary");
      // Close the menu once a destination is chosen.
      Array.prototype.forEach.call(menu.querySelectorAll("a"), function (link) {
        link.addEventListener("click", function () {
          menu.open = false;
        });
      });
      // Escape closes the menu and returns focus to its toggle.
      menu.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && menu.open) {
          menu.open = false;
          if (summary) {
            summary.focus();
          }
        }
      });
    });
  }

  function init() {
    setupTheme();
    setupTabs();
    setupCopy();
    setupMenu();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
