// Tiny DOM helpers — no framework, no build step.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of children) {
    node.append(child?.nodeType ? child : document.createTextNode(child ?? ""));
  }
  return node;
}

export const qs = (selector, root = document) => root.querySelector(selector);

let toastTimer;
export function toast(message) {
  const node = qs("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
}
