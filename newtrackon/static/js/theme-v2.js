// The NZ fork's design is dark-only (its colours are fixed), so the page is always in Bootstrap's dark mode:
// form fields, dropdowns and the API viewer then match the rest of the site.
(() => {
  "use strict";
  document.documentElement.setAttribute("data-bs-theme", "dark");
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("rapi-doc").forEach((apiDocs) => {
      apiDocs.setAttribute("theme", "dark");
      if (apiDocs.hasAttribute("data-primary-color-dark")) {
        apiDocs.setAttribute("primary-color", apiDocs.getAttribute("data-primary-color-dark"));
      }
    });
  }, { once: true });
})();
