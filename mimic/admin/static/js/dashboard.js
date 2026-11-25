// 1. Config Editor Line Numbers & Tab Handling
(function () {
  function initConfigEditor(wrap) {
    const gutter = wrap.querySelector(".module-config-gutter");
    const textarea = wrap.querySelector(".module-config-editor");
    if (!gutter || !textarea) return;

    function updateLines() {
      const lineCount = textarea.value.split("\n").length;
      gutter.textContent = Array.from({ length: lineCount }, (_, i) => i + 1).join("\n");
    }

    function syncScroll() { gutter.scrollTop = textarea.scrollTop; }

    textarea.addEventListener("input", updateLines);
    textarea.addEventListener("scroll", syncScroll);
    textarea.addEventListener("keydown", function (ev) {
      if (ev.key !== "Tab") return;
      ev.preventDefault();
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const value = textarea.value;

      if (ev.shiftKey) {
        const selected = value.substring(start, end);
        if (selected.indexOf("\n") !== -1) {
          const lines = selected.split("\n");
          let changed = false;
          const result = lines.map(line => {
            if (line.startsWith("\t")) { changed = true; return line.substring(1); }
            return line;
          });
          if (changed) {
            textarea.value = value.substring(0, start) + result.join("\n") + value.substring(end);
            textarea.selectionStart = start;
            textarea.selectionEnd = start + result.join("\n").length;
            updateLines();
            return;
          }
        }
        if (start > 0 && value[start - 1] === "\t") {
          textarea.value = value.substring(0, start - 1) + value.substring(end);
          textarea.selectionStart = textarea.selectionEnd = start - 1;
          updateLines();
        }
      } else {
        textarea.value = value.substring(0, start) + "\t" + value.substring(end);
        textarea.selectionStart = textarea.selectionEnd = start + 1;
        updateLines();
      }
    });
    updateLines();
  }

  document.querySelectorAll(".module-config-editor-wrap").forEach(initConfigEditor);

  // Module table row expansion
  const table = document.getElementById("module-table");
  if (table) {
    table.addEventListener("click", function (ev) {
      if (ev.target.closest(".module-toggle, .module-config-form, .module-config-editor-wrap, .infra-config-form")) return;
      const row = ev.target.closest("tr.module-row");
      if (!row || !table.contains(row)) return;
      const detailRow = row.nextElementSibling;
      if (!detailRow || !detailRow.classList.contains("module-row-detail")) return;
      const isHidden = detailRow.hidden;
      detailRow.hidden = !isHidden;
      row.classList.toggle("expanded", isHidden);
    });
  }

  // Module enable/disable switch: syncs the hidden field then submits the form.
  // (Replaces the inline onchange that a script-restricting CSP would block.)
  document.querySelectorAll(".module-toggle").forEach(function (form) {
    const checkbox = form.querySelector('input[type="checkbox"]');
    const hidden = form.querySelector('input[name="enabled"]');
    if (!checkbox || !hidden) return;
    checkbox.addEventListener("change", function () {
      hidden.value = checkbox.checked ? "1" : "0";
      form.submit();
    });
  });

  // AWS config toggle
  const useIamRoleCheckbox = document.querySelector('input[name="use_iam_role"]');
  const awsAccessKeyIdInput = document.getElementById('aws_access_key_id');
  const awsSecretAccessKeyInput = document.getElementById('aws_secret_access_key');
  if (useIamRoleCheckbox) {
    const toggleAwsInputs = () => {
      const disabled = useIamRoleCheckbox.checked;
      awsAccessKeyIdInput.disabled = disabled;
      awsSecretAccessKeyInput.disabled = disabled;
    };
    useIamRoleCheckbox.addEventListener('change', toggleAwsInputs);
    toggleAwsInputs(); // Initialize state
  }
})();

// 2. Config Panel Collapse Toggle
(function () {
  const configPanel = document.getElementById('config-panel-content');
  if (configPanel) {
    const panelHeading = configPanel.closest('.panel').querySelector('h2.config-toggle');
    if (panelHeading) {
      panelHeading.addEventListener('click', function(e) {
        if (e.target.closest('form') || e.target.closest('.switch')) return;
        configPanel.classList.toggle('collapsed');
        panelHeading.classList.toggle('collapsed');
      });
    }
  }
})();

// 3. Log Output & SSE Connection
(function () {
  const logOutput = document.getElementById('log-output');
  const logPanelContent = document.getElementById('log-panel-content');
  const logToggle = document.querySelector('h2.log-toggle');

  if (!logOutput) return;

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Global appendLog function — callable from anywhere in the page
  window.appendLog = function (message) {
    // [\s\S]* matches ANY character including newlines, fixing the multiline cutoff issue
    const match = message.match(/^\[(.*?)\] \[(INFO|DEBUG|WARNING|ERROR)\] ([\s\S]*)/);

    if (match) {
      const timestamp = match[1];
      const level = match[2];
      let content = match[3];
      
      let colorClass = 'log-info'; // default fallback
      switch (level) {
        case 'DEBUG': colorClass = 'log-debug'; break;
        case 'WARNING': colorClass = 'log-warning'; break;
        case 'ERROR':   colorClass = 'log-error'; break;
      }

      // Log content is attacker-controlled (request paths, headers, module ids):
      // escape before it goes anywhere near insertAdjacentHTML.
      const formattedContent = escapeHtml(content).replace(/\n/g, '<br>');

      logOutput.insertAdjacentHTML('beforeend',
        `<span class="${colorClass}">[${escapeHtml(timestamp)}] [${level}] ${formattedContent}</span><br>`
      );
    } else {
      logOutput.insertAdjacentHTML('beforeend', escapeHtml(message) + '\n');
    }

    // Auto-scroll to bottom
    logOutput.scrollTop = logOutput.scrollHeight;
  };

  // Collapsible toggle for Log Output header
  if (logToggle && logPanelContent) {
    logToggle.addEventListener('click', function (e) {
      if (e.target.closest('form') || e.target.closest('.switch')) return;
      logPanelContent.classList.toggle('collapsed');
      logToggle.classList.toggle('collapsed');
    });
  }

  const copyLogBtn = document.getElementById('copy-log-output');
  if (copyLogBtn) {
    copyLogBtn.addEventListener('click', function () {
      window.copyToClipboard(logOutput.textContent, copyLogBtn);
    });
  }

  // Live app-log updates. The broker pushes; there is no polling timer.
  if (typeof EventSource !== 'undefined') {
    const sse = window.mimicEvents || (window.mimicEvents = new EventSource('/admin/events'));
    sse.addEventListener('applog', function (ev) {
      try {
        const entry = JSON.parse(ev.data);
        window.appendLog(entry.text);
      } catch (_) {
        window.appendLog(ev.data);
      }
    });
    sse.addEventListener('resync', function () {
      window.appendLog('[log stream fell behind; reload to catch up]');
    });
    sse.onerror = function (err) {
      console.error('SSE connection error:', err);
    };
  }
})();
