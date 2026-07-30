document.addEventListener("DOMContentLoaded", () => {
  const grid = document.querySelector("[data-sort-grid]");
  const selector = document.querySelector("[data-sort-selector]");
  if (!grid || !selector) return;

  const cards = Array.from(grid.querySelectorAll("[data-sort-card]"));
  const saveUrl = grid.dataset.sortSaveUrl;

  const sortCards = () => {
    const option = selector.selectedOptions[0];
    if (!option) return;

    const { field, kind, direction } = option.dataset;
    if (!field) return;
    const directionMultiplier = direction === "desc" ? -1 : 1;

    cards.sort((leftCard, rightCard) => {
      const left = leftCard.dataset[field] ?? "";
      const right = rightCard.dataset[field] ?? "";

      if (kind === "number") {
        return (Number(left) - Number(right)) * directionMultiplier;
      }

      return left.localeCompare(right) * directionMultiplier;
    });

    cards.forEach((card) => grid.appendChild(card));
  };

  sortCards();
  grid.classList.remove("sort-pending");

  selector.addEventListener("change", async () => {
    sortCards();
    if (!saveUrl) return;

    try {
      const response = await fetch(saveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sort_order: selector.value }),
      });
      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
    } catch (error) {
      console.error("Could not save sort order", error);
    }
  });
});
