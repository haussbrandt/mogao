document.querySelectorAll("[data-library-switcher]").forEach((switcher) => {
  const trigger = switcher.querySelector("[data-library-switcher-trigger]");
  const menu = switcher.querySelector("[data-library-switcher-menu]");

  if (!trigger || !menu) return;

  const setOpen = (open) => {
    trigger.setAttribute("aria-expanded", String(open));
    menu.hidden = !open;
  };

  trigger.addEventListener("click", () => {
    setOpen(trigger.getAttribute("aria-expanded") !== "true");
  });

  trigger.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown") return;

    event.preventDefault();
    setOpen(true);
    menu.querySelector("a")?.focus();
  });

  switcher.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;

    setOpen(false);
    trigger.focus();
  });

  document.addEventListener("click", (event) => {
    if (!switcher.contains(event.target)) setOpen(false);
  });
});
