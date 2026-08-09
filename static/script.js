/* ===========================================================
   SmartReco — Shared Script
   One file, used by every page.
   =========================================================== */

document.addEventListener('DOMContentLoaded', () => {

  /* ---------- Toast helper ---------- */
  function showToast(message){
    let toast = document.querySelector('.sr-toast');
    if(!toast){
      toast = document.createElement('div');
      toast.className = 'sr-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('is-visible');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove('is-visible'), 2600);
  }
  window.srToast = showToast;

  /* ---------- Password show/hide ---------- */
  document.querySelectorAll('[data-toggle-password]').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById(btn.dataset.togglePassword);
      if(!input) return;
      const isHidden = input.type === 'password';
      input.type = isHidden ? 'text' : 'password';
      btn.classList.toggle('is-active', isHidden);
    });
  });

  /* ---------- Sliding Sign In / Sign Up panel ---------- */
  const slideContainer = document.querySelector('.sr-slide-container');
  if(slideContainer){
    document.querySelectorAll('[data-slide-to]').forEach(btn => {
      btn.addEventListener('click', () => {
        slideContainer.classList.toggle('is-active', btn.dataset.slideTo === 'signup');
      });
    });
  }

  /* ---------- Onboarding: topic selection ---------- */
  const topicGrid = document.querySelector('.sr-topic-grid');
  if(topicGrid){
    const continueBtn = document.getElementById('onboardContinue');
    const countLabel = document.getElementById('onboardCount');

    function refreshSelection(){
      const selected = topicGrid.querySelectorAll('.sr-topic.is-selected').length;
      if(continueBtn) continueBtn.disabled = selected === 0;
      if(countLabel) countLabel.textContent = selected + ' selected';
    }

    topicGrid.querySelectorAll('.sr-topic').forEach(topic => {
      topic.addEventListener('click', () => {
        topic.classList.toggle('is-selected');
        refreshSelection();
      });
    });

    refreshSelection();
  }

  /* ---------- Favorite / heart buttons on course cards ---------- */
  document.querySelectorAll('.sr-fav-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const courseId = btn.dataset.courseId;
      if(courseId){
        const fd = new FormData();
        fd.append('product_id', courseId);
        try {
          const res = await fetch('/api/wishlist/toggle', { method: 'POST', body: fd });
          const data = await res.json();
          btn.classList.toggle('is-active', data.saved);
          showToast(data.saved ? 'Saved to wishlist' : 'Removed from wishlist');
        } catch(err) {
          btn.classList.toggle('is-active');
        }
      } else {
        btn.classList.toggle('is-active');
      }
    });
  });

  /* ---------- Modal open/close ---------- */
  document.querySelectorAll('[data-open-modal]').forEach(btn => {
    btn.addEventListener('click', () => {
      const modal = document.getElementById(btn.dataset.openModal);
      if(modal) modal.classList.add('is-open');
    });
  });
  document.querySelectorAll('[data-close-modal]').forEach(btn => {
    btn.addEventListener('click', () => {
      const overlay = btn.closest('.sr-modal-overlay');
      if(overlay) overlay.classList.remove('is-open');
    });
  });
  document.querySelectorAll('.sr-modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
      if(e.target === overlay) overlay.classList.remove('is-open');
    });
  });
  document.addEventListener('keydown', (e) => {
    if(e.key === 'Escape'){
      document.querySelectorAll('.sr-modal-overlay.is-open').forEach(o => o.classList.remove('is-open'));
    }
  });

  /* ---------- Difficulty toggle group (Add Course modal) ---------- */
  document.querySelectorAll('.sr-diff-group').forEach(group => {
    const hiddenInput = document.getElementById('courseLevelInput');
    group.querySelectorAll('.sr-diff-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        group.querySelectorAll('.sr-diff-btn').forEach(b => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        if(hiddenInput) hiddenInput.value = btn.textContent.trim();
      });
    });
  });

  /* ---------- Tag input (Target Skills field) ---------- */
  document.querySelectorAll('.sr-tag-input').forEach(wrap => {
    const input = wrap.querySelector('input');
    const hiddenSkills = document.getElementById('courseSkillsInput');
    if(!input) return;

    function updateSkillsHidden(){
      if(!hiddenSkills) return;
      const chips = Array.from(wrap.querySelectorAll('.sr-tag-chip')).map(c => c.dataset.tagText);
      hiddenSkills.value = chips.join(', ');
    }

    function addTag(text){
      text = text.trim();
      if(!text) return;
      const chip = document.createElement('span');
      chip.className = 'sr-tag-chip';
      chip.dataset.tagText = text;
      chip.innerHTML = `${text} <button type="button" aria-label="Remove ${text}">&times;</button>`;
      chip.querySelector('button').addEventListener('click', () => {
        chip.remove();
        updateSkillsHidden();
      });
      wrap.insertBefore(chip, input);
      updateSkillsHidden();
    }

    input.addEventListener('keydown', (e) => {
      if(e.key === 'Enter' || e.key === ','){
        e.preventDefault();
        addTag(input.value);
        input.value = '';
      }
    });

    wrap.querySelectorAll('.sr-tag-chip button').forEach(b => {
      b.addEventListener('click', () => {
        b.closest('.sr-tag-chip').remove();
        updateSkillsHidden();
      });
    });
  });

  /* ---------- Add Course form submit ---------- */
  const addCourseForm = document.getElementById('addCourseForm');
  if(addCourseForm){
    addCourseForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const formData = new FormData(addCourseForm);
      try {
        const res = await fetch('/api/products', { method: 'POST', body: formData });
        if(res.ok){
          const overlay = addCourseForm.closest('.sr-modal-overlay');
          if(overlay) overlay.classList.remove('is-open');
          showToast('Course saved');
          window.location.reload();
        } else {
          showToast('Error saving course');
        }
      } catch(err) {
        showToast('Error connecting to server');
      }
    });
  }

  /* ---------- AI Live Sync toggle ---------- */
  const liveSync = document.getElementById('liveSyncToggle');
  if(liveSync){
    liveSync.addEventListener('click', async () => {
      const on = liveSync.getAttribute('aria-checked') === 'true';
      const newState = !on;
      liveSync.setAttribute('aria-checked', String(newState));
      liveSync.style.background = newState ? 'var(--success)' : 'var(--ink-300)';
      try {
        await fetch('/api/tracking-toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: newState })
        });
        showToast(`AI Live Sync ${newState ? 'Enabled' : 'Disabled'}`);
      } catch(err){}
    });
  }

  /* ---------- Refresh recommendations ---------- */
  const refreshBtn = document.getElementById('refreshRecs');
  if(refreshBtn){
    refreshBtn.addEventListener('click', async () => {
      showToast('Refreshing recommendations...');
      try {
        const res = await fetch('/api/ai/refresh', { method: 'POST' });
        if(res.ok){
          showToast('Recommendations updated!');
          setTimeout(() => window.location.reload(), 1000);
        }
      } catch(err){
        showToast('Error refreshing');
      }
    });
  }

  /* ---------- Table search filter (Admin panel) ---------- */
  const tableSearch = document.getElementById('courseSearch');
  if(tableSearch){
    tableSearch.addEventListener('input', () => {
      const q = tableSearch.value.trim().toLowerCase();
      document.querySelectorAll('#courseTable tbody tr').forEach(row => {
        const title = row.dataset.title || '';
        row.style.display = title.includes(q) ? '' : 'none';
      });
    });
  }

});
