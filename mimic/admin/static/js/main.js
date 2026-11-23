// Copies text to the clipboard and briefly swaps the button's copy icon for a
// check as feedback. Shared by the request log body blocks and the dashboard's
// Mimic Logs panel.
window.copyToClipboard = function (text, button) {
    if (button._copyTimer) return; // already showing the check feedback
    var iconEl = button.querySelector(".icon");

    function done() {
        iconEl.className = "icon icon-check";
        button._copyTimer = setTimeout(function () {
            iconEl.className = "icon icon-copy";
            button._copyTimer = null;
        }, 1500);
    }

    function fallback() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try {
            if (document.execCommand("copy")) done();
        } catch (e) {}
        document.body.removeChild(ta);
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
    } else {
        fallback();
    }
};

function onDocumentReady(fn) {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", fn);
    } else {
        fn();
    }
}

onDocumentReady(function () {
    setTimeout(function () {
        const messages = document.querySelectorAll('.flash');
        messages.forEach(function (msg) {
            msg.style.display = 'none';
        });
    }, 5000); // 5 seconds

    // The request-log filter applies as soon as a module is chosen. Kept here
    // rather than as an inline onchange so a script-restricting CSP can apply.
    const moduleFilterSelect = document.getElementById("module-filter");
    if (moduleFilterSelect) {
        moduleFilterSelect.addEventListener("change", function () {
            moduleFilterSelect.form.submit();
        });
    }

    // Clearing the whole request log is destructive, so confirm first.
    const clearLogForm = document.getElementById("clear-log-form");
    if (clearLogForm) {
        clearLogForm.addEventListener("submit", function (ev) {
            if (!window.confirm("Clear all log entries?")) ev.preventDefault();
        });
    }

    // Log table functionality
    // Check if we're on the logs page
    if (!document.getElementById("log-config")) {
        return;
    }

    var config = JSON.parse(document.getElementById("log-config").textContent);
    const rowsUrl = config.rowsUrl;
    const pollUrl = config.pollUrl;
    const eventsUrl = config.eventsUrl;
    const page = config.page;
    const perPage = config.perPage;
    let pages = config.pages;
    let totalCount = config.total;
    // Set from the initial page fetch: the newest row's id, or 0 if the page is empty.
    let maxId = 0;

    var tbody = document.getElementById("log-tbody");
    var meta = document.getElementById("logs-meta");
    var emptyEl = document.getElementById("logs-empty");
    if (!tbody) return;

    // Timestamps are stored in UTC; the toggle under the filter re-renders them
    // in the browser's local time. The preference persists across navigation.
    var localTimeEl = document.getElementById("local-time-toggle");
    var useLocalTime = false;
    try {
        useLocalTime = localStorage.getItem("mimic_logs_local_time") === "1";
    } catch (e) {}
    if (localTimeEl) localTimeEl.checked = useLocalTime;

    function localTimeText(ts) {
        // Match the UTC display (YYYY-MM-DD HH:MM:SS), in the browser's local time.
        return new Date(ts * 1000).toLocaleString("en-CA", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
            hour12: false, timeZoneName: "short",
        }).replace(", ", " ");
    }

    function formatTs(r) {
        if (useLocalTime && r.ts != null) return localTimeText(r.ts);
        return r.ts_display;
    }

    function syncMetaLine() {
        if (!meta) return;

        pages = Math.max(1, Math.ceil(totalCount / perPage));
        meta.textContent =
            totalCount + " entries · page " + page + " of " + pages;
    }

    function escapeHtml(s) {
        if (s == null || s === "") return "";

        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function isJSON(str) {
        try {
            JSON.parse(str);
            return true;
        } catch (e) {
            return false;
        }
    }

    function createHeadersTable(data) {
        var table = document.createElement("table");
        table.className = "headers-table";

        if (!data || typeof data !== "object") {
            return table;
        }

        var tbody = document.createElement("tbody");
        Object.entries(data).forEach(function (_ref) {
            var key = _ref[0], value = _ref[1];
            var row = document.createElement("tr");
            var keyCell = document.createElement("td");
            var valueCell = document.createElement("td");

            keyCell.textContent = key;
            valueCell.textContent = value;
            row.appendChild(keyCell);
            row.appendChild(valueCell);
            tbody.appendChild(row);
        });

        table.appendChild(tbody);
        return table;
    }

    function formatHeadersHtml(data) {
        return createHeadersTable(data).outerHTML;
    }

    function formatBodyValue(body) {
        if (body == null || body === "") {
            return "";
        }

        if (isJSON(body)) {
            try {
                var parsed = JSON.parse(body);
                return escapeHtml(JSON.stringify(parsed, null, 4));
            } catch (e) {
                // Fall through to escaped raw text.
            }
        }

        return escapeHtml(body);
    }

    // A body block: the pre plus a copy button floating in its top-right corner.
    function bodyPreHtml(body) {
        return '<div class="body-pre-wrap"><button type="button" class="btn copy-btn pre-copy" title="Copy" aria-label="Copy"><i class="icon icon-copy"></i></button><pre>' + formatBodyValue(body) + "</pre></div>";
    }

    function buildRow(r) {
        var mainTr = document.createElement("tr");
        mainTr.className = "log-row";
        mainTr.setAttribute("data-log-id", String(r.id));
        // Kept on the row so the local-time toggle can re-render existing rows.
        mainTr.dataset.ts = String(r.ts);
        mainTr.dataset.tsDisplay = r.ts_display;

        // Escaped once and reused for the cell's tooltip and the detail row.
        var fullPath = escapeHtml(r.path);
        if (r.query_string) fullPath += "?" + escapeHtml(r.query_string);
        var pathHtml = "<code>" + fullPath + "</code>";

        var moduleHtml = r.module_id ? "<code>" + escapeHtml(r.module_id) + "</code>" : "—";
        var statusHtml = r.response_status != null ? escapeHtml(String(r.response_status)) : "—";

        var parts = [];
        parts.push('<p class="muted">Path</p>');
        parts.push('<code class="full-path">' + fullPath + "</code>");
        // A body-unavailable note must not hide the headers, which are still logged.
        if (r.error) {
            parts.push('<span class="error">' + escapeHtml(r.error) + "</span>");
        }
        parts.push('<p class="muted">Request Headers</p>');
        parts.push(formatHeadersHtml(r.request_headers));
        if (r.request_body) {
            parts.push('<p class="muted">Request Body</p>');
            parts.push(bodyPreHtml(r.request_body));
        }
        parts.push('<p class="muted">Response Headers</p>');
        parts.push(formatHeadersHtml(r.response_headers));
        if (r.response_body) {
            parts.push('<p class="muted">Response Body</p>');
            parts.push(bodyPreHtml(r.response_body));
        }
        var detailHtml = parts.join("");

        mainTr.innerHTML =
            '<td class="log-cell-trigger">' + escapeHtml(formatTs(r)) + "</td>" +
            '<td class="log-cell-trigger">' + escapeHtml(r.method) + "</td>" +
            '<td class="log-cell-trigger" title="' + fullPath + '">' + pathHtml + "</td>" +
            '<td class="log-cell-trigger">' + moduleHtml + "</td>" +
            '<td class="log-cell-trigger">' + statusHtml + "</td>";

        var detailTr = document.createElement("tr");
        detailTr.className = "log-row-detail";
        detailTr.hidden = true;
        detailTr.innerHTML =
            '<td colspan="5" class="log-detail-cell">' + detailHtml + "</td>";

        return [mainTr, detailTr];
    }

    function trimOldRows() {
        if (page !== 1) return;
        while (tbody.rows.length > perPage * 2) {
            tbody.deleteRow(tbody.rows.length - 1);
        }
    }

    // A body block's resize handle must not shrink it below the size it first
    // rendered at, so pin that natural height as its min-height on first expand.
    // Rows start hidden (zero layout height), hence measuring here, not in buildRow.
    function lockInitialPreHeights(detailRow) {
        detailRow.querySelectorAll("pre").forEach(function (pre) {
            if (!pre.style.minHeight) pre.style.minHeight = pre.offsetHeight + "px";
        });
    }

    tbody.addEventListener("click", function (ev) {
        var copyBtn = ev.target.closest(".copy-btn");
        if (copyBtn) {
            var pre = copyBtn.parentElement.querySelector("pre");
            if (pre) window.copyToClipboard(pre.textContent, copyBtn);
            return;
        }

        var tr = ev.target.closest("tr.log-row");
        if (!tr || !tbody.contains(tr)) return;

        var detailRow = tr.nextElementSibling;
        if (!detailRow || !detailRow.classList.contains("log-row-detail")) return;

        ev.preventDefault();
        var expanded = detailRow.hidden === true;
        detailRow.hidden = !expanded;
        tr.classList.toggle("expanded", expanded);
        if (expanded) lockInitialPreHeights(detailRow);
    });

    if (localTimeEl) {
        localTimeEl.addEventListener("change", function () {
            useLocalTime = localTimeEl.checked;
            try {
                localStorage.setItem("mimic_logs_local_time", useLocalTime ? "1" : "0");
            } catch (e) {}
            tbody.querySelectorAll("tr.log-row").forEach(function (tr) {
                var ts = parseFloat(tr.dataset.ts);
                var text = (!isNaN(ts) && useLocalTime)
                    ? localTimeText(ts)
                    : tr.dataset.tsDisplay;
                tr.querySelector("td").textContent = text;
            });
        });
    }

    function currentModuleFilter() {
        return new URLSearchParams(window.location.search).get('module');
    }

    // Returns true if the row was actually prepended.
    function prependRow(r) {
        if (r.id <= maxId) return false;
        maxId = r.id;

        var moduleFilter = currentModuleFilter();
        if (moduleFilter && r.module_id !== moduleFilter) return false;

        var pair = buildRow(r);
        tbody.insertBefore(pair[1], tbody.firstChild);
        tbody.insertBefore(pair[0], tbody.firstChild);
        if (emptyEl) emptyEl.hidden = true;
        return true;
    }

    // Catch-up fetch: used once at startup and whenever the broker tells us the
    // stream fell behind.
    var resyncing = false;

    function resync() {
        if (page !== 1 || resyncing) return;
        resyncing = true;
        var u = pollUrl + "?after_id=" + encodeURIComponent(String(maxId));

        fetch(u, { credentials: "same-origin" })
            .then(function (res) {
                if (!res.ok) throw new Error("resync failed");
                return res.json();
            })
            .then(function (data) {
                // Server sends newest first; reverse so the newest ends up on top.
                var rows = (data.rows || []).slice().reverse();
                var added = rows.filter(prependRow).length;
                if (!added) return;

                // The poll total counts every module, so it is only usable unfiltered.
                if (currentModuleFilter()) {
                    totalCount += added;
                } else if (data.total != null) {
                    totalCount = data.total;
                }
                syncMetaLine();
                trimOldRows();
            })
            .catch(function (error) {
                console.error("Log resync error:", error);
            })
            .finally(function () {
                resyncing = false;
            });
    }

    function connectEvents() {
        var sse = window.mimicEvents || (window.mimicEvents = new EventSource(eventsUrl));

        sse.addEventListener("httplog", function (ev) {
            // A resync in flight will return these rows itself; applying them now
            // would advance maxId past rows the fetch has not delivered yet.
            if (resyncing) return;

            var row;
            try {
                row = JSON.parse(ev.data);
            } catch (err) {
                console.error("Malformed httplog event:", err);
                return;
            }
            if (!prependRow(row)) return;
            totalCount += 1;
            syncMetaLine();
            trimOldRows();
        });

        sse.addEventListener("resync", resync);
        sse.onerror = function (err) {
            console.error("SSE connection error:", err);
        };

        // Anything logged between page render and stream connect.
        resync();
    }

    // Render the initial page, then connect to the live stream. Every row is built
    // by buildRow(), so fetched rows and live rows can never diverge in formatting
    // (the bug that used to hit server-rendered rows after a refresh).
    function loadInitialPage() {
        var u = rowsUrl + "?page=" + page + "&per=" + perPage;
        var moduleFilter = currentModuleFilter();
        if (moduleFilter) u += "&module=" + encodeURIComponent(moduleFilter);

        fetch(u, { credentials: "same-origin" })
            .then(function (res) {
                if (!res.ok) throw new Error("initial log fetch failed");
                return res.json();
            })
            .then(function (data) {
                var rows = data.rows || []; // newest first, as served
                if (data.total != null) totalCount = data.total;
                maxId = rows.length ? rows[0].id : 0;

                // tbody is empty; appending in server order puts the newest row on top.
                rows.forEach(function (r) {
                    var pair = buildRow(r);
                    tbody.appendChild(pair[0]);
                    tbody.appendChild(pair[1]);
                });
                if (emptyEl) emptyEl.hidden = rows.length > 0;
                syncMetaLine();

                if (page === 1) connectEvents();
            })
            .catch(function (error) {
                console.error("Log load error:", error);
                // Degrade to the resync path: maxId is still 0, so resync re-fetches
                // the newest rows and live events keep flowing.
                if (page === 1) connectEvents();
            });
    }

    loadInitialPage();
});