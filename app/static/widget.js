/**
 * SwiftForms Widget — Embeddable form-capture script for SwiftAgent.
 *
 * Usage (paste into any page):
 *
 *   <script src="https://swiftagents.org/api/v1/public/forms/widget.js"></script>
 *   <script>
 *     SwiftForms.init({ publicKey: "SDPK-272XXXXXXXXXXXX" });
 *   </script>
 *
 * No external dependencies. If SwiftAgent is unreachable the original form
 * submission is never blocked.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ */
  /*  Internal state                                                     */
  /* ------------------------------------------------------------------ */
  var _publicKey = null;
  var _apiBase = null;
  var _initialized = false;
  var _boundForms = new WeakSet(); // track forms we already attached to
  var _formCounter = 0; // fallback naming counter

  /* ------------------------------------------------------------------ */
  /*  Helpers                                                            */
  /* ------------------------------------------------------------------ */

  /**
   * Generate a v4-style random UUID.
   */
  function uuidv4() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(
      /[xy]/g,
      function (c) {
        var r = (Math.random() * 16) | 0;
        var v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }
    );
  }

  /**
   * Get or create a persistent visitor id stored in localStorage.
   */
  function getVisitorId() {
    var key = "swift_visitor_id";
    try {
      var id = localStorage.getItem(key);
      if (!id) {
        id = uuidv4();
        localStorage.setItem(key, id);
      }
      return id;
    } catch (_) {
      // localStorage unavailable (e.g. Safari private mode) — per-page id
      return uuidv4();
    }
  }

  /**
   * Auto-detect the API base URL from the <script> tag that loaded this file.
   * We look for a <script src> whose path ends with the known widget path and
   * strip that suffix to derive the origin.
   */
  function detectApiBase() {
    var WIDGET_PATH = "/api/v1/public/forms/widget.js";
    var scripts = document.querySelectorAll("script[src]");
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].getAttribute("src") || "";
      // Normalise relative URLs
      try {
        var url = new URL(src, window.location.href);
        if (url.pathname.indexOf(WIDGET_PATH) !== -1) {
          // Strip the widget path to get the base
          return url.origin + url.pathname.replace(WIDGET_PATH, "");
        }
      } catch (_) {
        // Ignore malformed URLs
      }
    }
    // Fallback: assume same origin
    return window.location.origin;
  }

  /* ------------------------------------------------------------------ */
  /*  Form name resolution                                               */
  /* ------------------------------------------------------------------ */

  /**
   * Derive a human-readable name for a form.
   *
   * Priority:
   *   1. data-swift-name attribute (developer override)
   *   2. title or aria-label attribute
   *   3. Nearest preceding <h1>–<h4> heading
   *   4. id or name attribute
   *   5. Fallback: "Form N"
   */
  function resolveFormName(form) {
    // 1. Explicit override
    var swiftName = form.getAttribute("data-swift-name");
    if (swiftName && swiftName.trim()) return swiftName.trim();

    // 2. Standard accessible attributes
    var title = form.getAttribute("title");
    if (title && title.trim()) return title.trim();
    var ariaLabel = form.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

    // 3. Nearest heading above the form
    var headingText = findNearestHeading(form);
    if (headingText) return headingText;

    // 4. id / name
    if (form.id && form.id.trim()) return form.id.trim();
    if (form.name && form.name.trim()) return form.name.trim();

    // 5. Fallback
    _formCounter++;
    return "Form " + _formCounter;
  }

  /**
   * Walk backwards through previous siblings / parent siblings looking for
   * the nearest <h1>–<h4>.
   */
  function findNearestHeading(form) {
    var HEADING_RE = /^H[1-4]$/i;
    var node = form;

    while (node) {
      // Check previous siblings
      var sibling = node.previousElementSibling;
      while (sibling) {
        if (HEADING_RE.test(sibling.tagName)) {
          var text = (sibling.textContent || "").trim();
          if (text) return text;
        }
        sibling = sibling.previousElementSibling;
      }
      // Move up to parent and keep searching
      node = node.parentElement;
    }
    return null;
  }

  /* ------------------------------------------------------------------ */
  /*  Form identifier                                                    */
  /* ------------------------------------------------------------------ */

  /**
   * Build a stable identifier string for a form.
   * Uses id → name → action → DOM index, in priority order.
   */
  function buildFormIdentifier(form) {
    if (form.id) return "id:" + form.id;
    if (form.name) return "name:" + form.name;
    var action = form.getAttribute("action");
    if (action && action.trim()) return "action:" + action.trim();
    // DOM index fallback
    var allForms = document.querySelectorAll("form");
    for (var i = 0; i < allForms.length; i++) {
      if (allForms[i] === form) return "index:" + i;
    }
    return "index:unknown";
  }

  /* ------------------------------------------------------------------ */
  /*  Field collection                                                   */
  /* ------------------------------------------------------------------ */

  /**
   * Collect all submittable field values from a form.
   *
   * Rules:
   *  - Skip password fields (type="password")
   *  - Skip hidden CSRF-style tokens (type="hidden" with common CSRF names)
   *  - Skip file inputs
   *  - Use field name as key, fallback to id
   *  - Handle checkbox groups, radio buttons, <select multiple>
   */
  function collectFields(form) {
    var data = {};
    var CSRF_NAMES = [
      "csrfmiddlewaretoken",
      "_csrf",
      "_token",
      "csrf_token",
      "authenticity_token",
      "csrftoken",
      "__requestverificationtoken",
    ];

    var elements = form.elements;
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      var tag = el.tagName.toUpperCase();
      var type = (el.type || "").toLowerCase();
      var key = el.name || el.id;

      // Must have an identifiable key
      if (!key) continue;

      // Skip passwords
      if (type === "password") continue;

      // Skip file inputs
      if (type === "file") continue;

      // Skip hidden CSRF tokens
      if (type === "hidden" && CSRF_NAMES.indexOf(key.toLowerCase()) !== -1) {
        continue;
      }

      // Skip submit / button / reset / image (not user data)
      if (
        type === "submit" ||
        type === "button" ||
        type === "reset" ||
        type === "image"
      ) {
        continue;
      }

      // --- Handle specific input types ---

      if (type === "checkbox") {
        // Checkbox group: collect checked values into an array
        if (!data.hasOwnProperty(key)) {
          data[key] = [];
        }
        if (el.checked) {
          // If value is the default "on" just push true, otherwise push value
          data[key].push(el.value === "on" ? true : el.value);
        }
        continue;
      }

      if (type === "radio") {
        // Only capture the selected radio
        if (el.checked) {
          data[key] = el.value;
        } else if (!data.hasOwnProperty(key)) {
          data[key] = null; // none selected yet
        }
        continue;
      }

      if (tag === "SELECT" && el.multiple) {
        // <select multiple> — collect all selected options
        var selected = [];
        for (var j = 0; j < el.options.length; j++) {
          if (el.options[j].selected) {
            selected.push(el.options[j].value);
          }
        }
        data[key] = selected;
        continue;
      }

      // Default: text, email, number, textarea, select (single), etc.
      data[key] = el.value;
    }

    return data;
  }

  /* ------------------------------------------------------------------ */
  /*  Form binding                                                       */
  /* ------------------------------------------------------------------ */

  /**
   * Attach our capture listener to a single <form>.
   */
  function bindForm(form) {
    // Skip if already bound
    if (_boundForms.has(form)) return;

    // Honour data-swift-ignore
    if (form.hasAttribute("data-swift-ignore")) return;

    _boundForms.add(form);

    var formName = resolveFormName(form);
    var formIdentifier = buildFormIdentifier(form);

    form.addEventListener("submit", function (e) {
      // We do NOT prevent default here (e.preventDefault()). 
      // This ensures we do not break Single Page Apps (React/Next.js) or standard native submissions.

      var payload = {
        page_url: window.location.href,
        form_identifier: formIdentifier,
        form_name: formName,
        data: collectFields(form),
        visitor_id: getVisitorId(),
      };

      // Pass public key in query string to avoid custom headers
      var endpoint = _apiBase + "/api/v1/public/forms/widget/submit?public_key=" + encodeURIComponent(_publicKey);

      try {
        fetch(endpoint, {
          method: "POST",
          // Send as default text/plain to avoid CORS preflight, allowing keepalive to work.
          // The backend manually parses the JSON body.
          body: JSON.stringify(payload),
          keepalive: true,
        }).catch(function () {
          // Silently ignore network errors to not pollute client console
        });
      } catch (_) {
        // Silently ignore synchronous fetch errors
      }
    });
  }

  /**
   * Scan the DOM for all <form> elements and bind any new ones.
   */
  function scanAndBind() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      bindForm(forms[i]);
    }
  }

  /* ------------------------------------------------------------------ */
  /*  MutationObserver — catch dynamically added forms (SPAs)            */
  /* ------------------------------------------------------------------ */

  function startObserver() {
    if (typeof MutationObserver === "undefined") return;

    var observer = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var node = added[j];
          if (node.nodeType !== 1) continue; // element nodes only

          // The added node itself might be a form
          if (node.tagName === "FORM") {
            bindForm(node);
          }

          // Or it might contain forms deeper in its subtree
          if (node.querySelectorAll) {
            var nested = node.querySelectorAll("form");
            for (var k = 0; k < nested.length; k++) {
              bindForm(nested[k]);
            }
          }
        }
      }
    });

    observer.observe(document.body, { childList: true, subtree: true });
  }

  /* ------------------------------------------------------------------ */
  /*  Public API                                                         */
  /* ------------------------------------------------------------------ */

  window.SwiftForms = {
    /**
     * Initialise the widget.
     *
     * @param {Object} config
     * @param {string} config.publicKey — Your SwiftAgent public key (SDPK-…)
     */
    init: function (config) {
      if (_initialized) {
        console.warn("[SwiftForms] Already initialised.");
        return;
      }

      if (!config || !config.publicKey) {
        console.error("[SwiftForms] publicKey is required.");
        return;
      }

      _publicKey = config.publicKey;
      _apiBase = detectApiBase();
      _initialized = true;

      // Bind as soon as the DOM is ready
      if (
        document.readyState === "interactive" ||
        document.readyState === "complete"
      ) {
        scanAndBind();
        startObserver();
      } else {
        document.addEventListener("DOMContentLoaded", function () {
          scanAndBind();
          startObserver();
        });
      }
    },
  };
})();
