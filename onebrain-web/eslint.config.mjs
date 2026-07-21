import nextVitals from "eslint-config-next/core-web-vitals";

// eslint-plugin-jsx-a11y's recommended set.
//
// The plugin is NOT a declared dependency of this package and is not hoisted to
// the top-level node_modules — it is installed under
// node_modules/eslint-config-next/node_modules/, so `import jsxA11y from
// "eslint-plugin-jsx-a11y"` would not resolve here. This repo pins exact
// versions behind a hash-locked install, so adding a direct dependency just to
// spread `jsxA11y.flatConfigs.recommended` is not worth it. Instead we reuse the
// `jsx-a11y` plugin namespace that eslint-config-next already registers (see its
// `next` config object) and turn the recommended rules on by name. Flat config
// merges `plugins` across every config object that matches a file, so the rule
// names below resolve as long as `files` mirrors the `next` config's pattern.
//
// Kept in sync with eslint-plugin-jsx-a11y 6.10.2 `flatConfigs.recommended`.
// The three rules recommended leaves off (anchor-ambiguous-text,
// control-has-associated-label, label-has-for) are omitted rather than listed
// as "off". Entries with options mirror the plugin's own defaults; the rest of
// the list overrides the five rules eslint-config-next only sets to "warn".
const jsxA11yRecommended = {
  files: ["**/*.{js,jsx,mjs,ts,tsx,mts,cts}"],
  name: "onebrain/jsx-a11y-recommended",
  rules: {
    "jsx-a11y/alt-text": "error",
    "jsx-a11y/anchor-has-content": "error",
    "jsx-a11y/anchor-is-valid": "error",
    "jsx-a11y/aria-activedescendant-has-tabindex": "error",
    "jsx-a11y/aria-props": "error",
    "jsx-a11y/aria-proptypes": "error",
    "jsx-a11y/aria-role": "error",
    "jsx-a11y/aria-unsupported-elements": "error",
    "jsx-a11y/autocomplete-valid": "error",
    "jsx-a11y/click-events-have-key-events": "error",
    "jsx-a11y/heading-has-content": "error",
    "jsx-a11y/html-has-lang": "error",
    "jsx-a11y/iframe-has-title": "error",
    "jsx-a11y/img-redundant-alt": "error",
    "jsx-a11y/interactive-supports-focus": [
      "error",
      { tabbable: ["button", "checkbox", "link", "searchbox", "spinbutton", "switch", "textbox"] },
    ],
    // depth 3 (not the rule's default 2) because this codebase's standard
    // labelled-control markup is `<label><input/><span><strong>Title</strong>
    // <small>Hint</small></span></label>`. Those labels do wrap their control
    // and do carry text; the text just sits one level deeper than the default
    // allows, so the default reports them as unlabelled.
    "jsx-a11y/label-has-associated-control": ["error", { depth: 3 }],
    "jsx-a11y/media-has-caption": "error",
    "jsx-a11y/mouse-events-have-key-events": "error",
    "jsx-a11y/no-access-key": "error",
    "jsx-a11y/no-autofocus": "error",
    "jsx-a11y/no-distracting-elements": "error",
    "jsx-a11y/no-interactive-element-to-noninteractive-role": [
      "error",
      { canvas: ["img"], tr: ["none", "presentation"] },
    ],
    "jsx-a11y/no-noninteractive-element-interactions": [
      "error",
      {
        alert: ["onKeyUp", "onKeyDown", "onKeyPress"],
        body: ["onError", "onLoad"],
        dialog: ["onKeyUp", "onKeyDown", "onKeyPress"],
        handlers: ["onClick", "onError", "onLoad", "onMouseDown", "onMouseUp", "onKeyPress", "onKeyDown", "onKeyUp"],
        iframe: ["onError", "onLoad"],
        img: ["onError", "onLoad"],
      },
    ],
    "jsx-a11y/no-noninteractive-element-to-interactive-role": [
      "error",
      {
        fieldset: ["radiogroup", "presentation"],
        li: ["menuitem", "menuitemradio", "menuitemcheckbox", "option", "row", "tab", "treeitem"],
        ol: ["listbox", "menu", "menubar", "radiogroup", "tablist", "tree", "treegrid"],
        table: ["grid"],
        td: ["gridcell"],
        ul: ["listbox", "menu", "menubar", "radiogroup", "tablist", "tree", "treegrid"],
      },
    ],
    "jsx-a11y/no-noninteractive-tabindex": [
      "error",
      { allowExpressionValues: true, roles: ["tabpanel"], tags: [] },
    ],
    "jsx-a11y/no-redundant-roles": "error",
    "jsx-a11y/no-static-element-interactions": [
      "error",
      {
        allowExpressionValues: true,
        handlers: ["onClick", "onMouseDown", "onMouseUp", "onKeyPress", "onKeyDown", "onKeyUp"],
      },
    ],
    "jsx-a11y/role-has-required-aria-props": "error",
    "jsx-a11y/role-supports-aria-props": "error",
    "jsx-a11y/scope": "error",
    "jsx-a11y/tabindex-no-positive": "error",
  },
};

const eslintConfig = [...nextVitals, jsxA11yRecommended];

export default eslintConfig;
