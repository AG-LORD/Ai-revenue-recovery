/* Dashboard enhancement: expose approved bounded retries as customer checkout actions. */
(function () {
  function decorateRetryActions() {
    const tbody = document.getElementById("cases-tbody");
    if (!tbody) return;

    tbody.querySelectorAll("tr").forEach((row) => {
      const cells = row.querySelectorAll("td");
      if (cells.length < 10) return;

      const status = (cells[6].innerText || "").trim();
      const actionCell = cells[7];
      if (!actionCell || !status.includes("RECOVERING")) return;
      if (!actionCell.innerText.includes("Bounded Retry")) return;
      if (actionCell.querySelector(".retry-checkout-btn")) return;

      const paymentId = cells[1]?.querySelector("span")?.innerText?.trim();
      if (!paymentId) return;

      const link = document.createElement("a");
      link.className = "plink-btn retry-checkout-btn";
      link.href = `/retry/${encodeURIComponent(paymentId)}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.innerText = "↻ Open Retry Checkout";
      link.title = "Open the approved bounded retry checkout";

      actionCell.replaceChildren(link);
    });
  }

  function observeDashboardRows() {
    const tbody = document.getElementById("cases-tbody");
    if (!tbody) {
      setTimeout(observeDashboardRows, 100);
      return;
    }

    decorateRetryActions();
    new MutationObserver(decorateRetryActions).observe(tbody, { childList: true, subtree: true });
  }

  observeDashboardRows();
})();
