/*

  SmartReco behavioral tracker.

  - Batches events in memory, flushes every 5s via fetch()

  - Early flush at 10 queued events

  - Force-flushes via sendBeacon on tab hide/close

  - Respects window.__SMARTRECO_TRACKING_ENABLED__ (default true)

  - Delegated click/dismiss tracking via data attributes

*/

(function () {

  const ENDPOINT = "/api/events";

  const FLUSH_INTERVAL_MS = 5000;

  const MAX_QUEUE_BEFORE_EARLY_FLUSH = 10;

  const MAX_QUEUE = 200;



  let queue = [];

  const pageLoadTime = performance.now();



  function trackingEnabled() {

    return window.__SMARTRECO_TRACKING_ENABLED__ !== false;

  }



  function pushEvent(eventType, productId, metadata) {

    if (!trackingEnabled()) return;

    if (queue.length >= MAX_QUEUE) {

      queue.shift(); // Bound queue size to prevent memory leaks

    }

    queue.push({

      event_type: eventType,

      product_id: productId || null,

      metadata: metadata || {},

      client_timestamp: new Date().toISOString(),

    });

    if (queue.length >= MAX_QUEUE_BEFORE_EARLY_FLUSH) {

      scheduleFlush(false);

    }

  }



  function scheduleFlush(useBeacon) {

    if (typeof requestIdleCallback === "function") {

      requestIdleCallback(() => flush(useBeacon), { timeout: 2000 });

    } else {

      setTimeout(() => flush(useBeacon), 0);

    }

  }



  function flush(useBeacon) {

    if (!trackingEnabled() || queue.length === 0) return;

    const batch = queue.splice(0, queue.length);

    const payload = JSON.stringify({ events: batch });



    if (useBeacon && navigator.sendBeacon) {

      const blob = new Blob([payload], { type: "application/json" });

      const queued = navigator.sendBeacon(ENDPOINT, blob);

      if (!queued) {

        fetch(ENDPOINT, {

          method: "POST",

          headers: { "Content-Type": "application/json" },

          body: payload,

          keepalive: true,

        }).catch(() => {

          queue.unshift(...batch);

        });

      }

    } else {

      fetch(ENDPOINT, {

        method: "POST",

        headers: { "Content-Type": "application/json" },

        body: payload,

        keepalive: true,

      }).catch(() => {

        queue.unshift(...batch);

        console.warn("SmartReco: event flush failed, re-queued batch");

      });

    }

  }



  let timeSpentRecorded = false;



  function recordTimeSpent() {

    if (timeSpentRecorded || !trackingEnabled()) return;

    timeSpentRecorded = true;

    const seconds = Math.round((performance.now() - pageLoadTime) / 1000);

    if (seconds > 0) {

      pushEvent("time_spent", window.__SMARTRECO_PRODUCT_ID__ || null, {

        seconds,

        path: location.pathname,

      });

    }

  }



  function parseProductId(el) {

    const raw = el.dataset.productId;

    if (!raw) return null;

    const id = parseInt(raw, 10);

    return Number.isFinite(id) ? id : null;

  }



  document.addEventListener("click", (e) => {

    const dismissEl = e.target.closest("[data-sr-track-dismiss]");

    if (dismissEl) {

      e.preventDefault();

      e.stopPropagation();

      const productId = parseProductId(dismissEl);

      if (productId) {

        pushEvent("dismiss", productId, { source: dismissEl.dataset.source || "recommendation" });

        const card = dismissEl.closest(".sr-insight-card, .sr-pick-card");

        if (card) card.style.opacity = "0.45";

        dismissEl.disabled = true;

        dismissEl.textContent = "Hidden";

      }

      return;

    }



    const clickEl = e.target.closest("[data-sr-track-click]");

    if (clickEl) {

      const productId = parseProductId(clickEl);

      pushEvent("click", productId, {

        source: clickEl.dataset.source || "unknown",

      });

    }

  });



  const firedScrollMilestones = {};

  const handleScroll = typeof throttle === "function" ? throttle(function () {

    if (!trackingEnabled()) return;

    const docHeight = Math.max(

      document.body.scrollHeight, document.documentElement.scrollHeight,

      document.body.offsetHeight, document.documentElement.offsetHeight,

      document.body.clientHeight, document.documentElement.clientHeight

    );

    const winHeight = window.innerHeight;

    if (docHeight <= winHeight) return;



    const scrollPos = window.scrollY || window.pageYOffset || document.documentElement.scrollTop;

    const pct = Math.round(((scrollPos + winHeight) / docHeight) * 100);



    const milestones = [25, 50, 75, 100];

    for (let i = 0; i < milestones.length; i++) {

      const m = milestones[i];

      if (pct >= m && !firedScrollMilestones[m]) {

        firedScrollMilestones[m] = true;

        pushEvent("scroll_depth", window.__SMARTRECO_PRODUCT_ID__ || null, {

          value: m,

          path: location.pathname,

        });

      }

    }

  }, 300) : null;



  if (handleScroll) {

    window.addEventListener("scroll", handleScroll, { passive: true });

  }



  setInterval(() => scheduleFlush(false), FLUSH_INTERVAL_MS);



  document.addEventListener("visibilitychange", () => {

    if (document.visibilityState === "hidden") {

      recordTimeSpent();

      flush(true);

    }

  });

  window.addEventListener("pagehide", () => {

    recordTimeSpent();

    flush(true);

  });



  if (trackingEnabled()) {

    pushEvent("view", window.__SMARTRECO_PRODUCT_ID__ || null, {

      path: location.pathname,

    });

  }



  window.SmartRecoTracker = { track: pushEvent, flush: () => flush(false) };

})();

