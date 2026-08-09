/**

 * Reusable debounce — resets timer on every call, fires fn after delay ms of inactivity.

 */

function debounce(fn, delay = 1000) {

  let timer = null;

  const debounced = function (...args) {

    clearTimeout(timer);

    timer = setTimeout(() => fn.apply(this, args), delay);

  };

  debounced.cancel = () => clearTimeout(timer);

  debounced.flush = function (...args) {

    clearTimeout(timer);

    return fn.apply(this, args);

  };

  return debounced;

}



/**

 * Simple throttle — fires fn at most once per intervalMs.

 */

function throttle(fn, intervalMs = 1000) {

  let last = 0;

  return function (...args) {

    const now = Date.now();

    if (now - last >= intervalMs) {

      last = now;

      return fn.apply(this, args);

    }

  };

}

