function addField() {
  document
    .getElementById("fields")
    .insertAdjacentHTML(
      "beforeend",
      '<div class="row"><input name="field_name" placeholder="Field name"><select name="field_type"><option>text</option><option>number</option><option>decimal</option><option>date</option></select><input name="field_unit" placeholder="unit"><button type="button" onclick="this.parentElement.remove()">×</button></div>',
    );
}
function addAddon() {
  document
    .getElementById("addons")
    .insertAdjacentHTML(
      "beforeend",
      '<div class="row"><input name="addon_name" placeholder="Add-on name"><input name="addon_price" type="number" step="0.01" placeholder="Price"><button type="button" onclick="this.parentElement.remove()">×</button></div>',
    );
}
function fillPrice(s) {
  let o = s.options[s.selectedIndex];
  let row = s.closest(".sale-item");
  row.querySelector('[name="unit_price"]').value = o.dataset.price || "";
}

// Remember nav group open state and list filters across pages.
document.querySelectorAll("details[data-nav-group]").forEach((group) => {
  const key = "nav-group:" + group.dataset.navGroup;
  if (!group.open && localStorage.getItem(key) === "1") group.open = true;
  group.addEventListener("toggle", () => localStorage.setItem(key, group.open ? "1" : "0"));
});

// Saved query (filters, search, sort) is restored when a list page opens without one.
if (document.querySelector("form.sales-filters, form.admin-user-filters, form.list-search")) {
  const key = "filters:" + location.pathname;
  const saved = localStorage.getItem(key);
  if (!location.search && saved) {
    location.replace(location.pathname + saved);
  } else if (location.search) {
    const params = new URLSearchParams(location.search);
    params.delete("page");
    localStorage.setItem(key, "?" + params.toString());
  }
  document
    .querySelectorAll("form.sales-filters a.secondary, form.admin-user-filters a.secondary, form.list-search a.secondary")
    .forEach((link) => link.addEventListener("click", () => localStorage.removeItem(key)));
}

document.querySelectorAll("a.logout-link").forEach((link) =>
  link.addEventListener("click", () => {
    Object.keys(localStorage)
      .filter((k) => k.startsWith("filters:"))
      .forEach((k) => localStorage.removeItem(k));
  }),
);

document.querySelectorAll('input[type="password"]').forEach((input) => {
  const wrapper = document.createElement("span");
  wrapper.className = "password-input-wrap";
  input.parentNode.insertBefore(wrapper, input);
  wrapper.appendChild(input);

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "password-visibility-toggle";
  toggle.textContent = "Show";
  toggle.setAttribute("aria-label", "Show password");
  toggle.setAttribute("aria-pressed", "false");
  toggle.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    toggle.textContent = show ? "Hide" : "Show";
    toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
    toggle.setAttribute("aria-pressed", String(show));
  });
  wrapper.appendChild(toggle);
});
