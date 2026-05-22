/**
 * kpi.js — KPIテーブル操作（ソート、フィルタ、コピー、エクスポート）
 */

let dataTable = null;

/**
 * DataTables 初期化
 */
function initKpiTable() {
    var table = document.getElementById('kpi-table');
    if (!table) return;

    dataTable = $('#kpi-table').DataTable({
        paging: true,
        pageLength: 50,
        lengthMenu: [25, 50, 100, 200, -1],
        ordering: true,
        searching: true,
        info: true,
        scrollX: true,
        autoWidth: false,
        language: {
            lengthMenu: '_MENU_ rows/page',
            info: 'Showing _START_ to _END_ of _TOTAL_ entries',
            search: 'Search:',
            paginate: {
                first: 'First',
                last: 'Last',
                next: 'Next',
                previous: 'Prev'
            }
        },
        select: false,
    });

    // 行クリックで選択/解除（複数選択対応）
    $('#kpi-table tbody').on('click', 'tr', function(e) {
        if (e.ctrlKey || e.metaKey) {
            $(this).toggleClass('table-primary selected');
        } else if (e.shiftKey) {
            var allRows = $('#kpi-table tbody tr');
            var lastSelected = allRows.filter('.selected').last();
            if (lastSelected.length) {
                var startIdx = allRows.index(lastSelected);
                var endIdx = allRows.index(this);
                var minIdx = Math.min(startIdx, endIdx);
                var maxIdx = Math.max(startIdx, endIdx);
                allRows.slice(minIdx, maxIdx + 1).addClass('table-primary selected');
            } else {
                $(this).addClass('table-primary selected');
            }
        } else {
            $('#kpi-table tbody tr').removeClass('table-primary selected');
            $(this).addClass('table-primary selected');
        }
    });
}

/**
 * 現在のページURLパラメータを構築
 */
function buildParams(overrides) {
    var params = new URLSearchParams();
    params.set('start', document.getElementById('param-start').value);
    params.set('end', document.getElementById('param-end').value);
    params.set('ids', document.getElementById('param-ids').value);
    params.set('mode', document.getElementById('mode-select').value);
    params.set('show_all_cols', document.getElementById('chk-all-cols').checked);
    params.set('show_all_data', document.getElementById('chk-all-data').checked);
    params.set('max_rows', document.getElementById('max-rows-select').value);

    if (overrides) {
        for (var key in overrides) {
            if (overrides.hasOwnProperty(key)) {
                params.set(key, overrides[key]);
            }
        }
    }
    return params;
}

/**
 * モード変更
 */
function changeMode() {
    var params = buildParams();
    saveSettings();
    window.location.href = '/kpi?' + params.toString();
}

/**
 * 設定変更時（チェックボックス、Max Rows）
 */
function onSettingChanged() {
    saveSettings();
    var params = buildParams();
    window.location.href = '/kpi?' + params.toString();
}

/**
 * 設定をサーバーに保存
 */
function saveSettings() {
    var data = {
        showAllColumns: document.getElementById('chk-all-cols').checked,
        showAllData: document.getElementById('chk-all-data').checked,
        maxRows: parseInt(document.getElementById('max-rows-select').value, 10),
    };

    fetch('/api/kpi_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    }).catch(function() {});
}

/**
 * データ再取得（リロード）
 */
function reloadKPI() {
    var params = buildParams();
    window.location.href = '/kpi?' + params.toString();
}

/**
 * 数値範囲フィルタ適用
 */
function applyFilter() {
    if (!dataTable) return;

    var colName = document.getElementById('filter-col').value;
    var minVal = parseFloat(document.getElementById('filter-min').value);
    var maxVal = parseFloat(document.getElementById('filter-max').value);

    if (!colName) {
        dataTable.search('').draw();
        return;
    }

    var headers = dataTable.columns().header().toArray();
    var colIdx = -1;
    for (var i = 0; i < headers.length; i++) {
        if (headers[i].textContent.trim() === colName) {
            colIdx = i;
            break;
        }
    }

    if (colIdx < 0) return;

    $.fn.dataTable.ext.search.length = 0;
    $.fn.dataTable.ext.search.push(function(settings, data, dataIndex) {
        var val = parseFloat(data[colIdx]);
        if (isNaN(val)) return false;
        if (!isNaN(minVal) && val < minVal) return false;
        if (!isNaN(maxVal) && val > maxVal) return false;
        return true;
    });

    dataTable.draw();
}

/**
 * フィルタクリア
 */
function clearFilter() {
    document.getElementById('filter-col').value = '';
    document.getElementById('filter-min').value = '';
    document.getElementById('filter-max').value = '';
    if (dataTable) {
        $.fn.dataTable.ext.search.length = 0;
        dataTable.draw();
    }
}

/**
 * 選択行をクリップボードにコピー（TSV形式）
 */
function copySelectedRows() {
    var table = document.getElementById('kpi-table');
    if (!table) return;

    var selected = table.querySelectorAll('tbody tr.selected');
    if (selected.length === 0) {
        showBottomBar('Select rows first: click a row to select, Ctrl+click for multiple', 'warning');
        return;
    }

    // ヘッダー
    var headerCells = table.querySelectorAll('thead th');
    var headers = [];
    for (var i = 0; i < headerCells.length; i++) {
        headers.push(headerCells[i].textContent.trim());
    }

    // 行データ
    var lines = [headers.join('\t')];
    for (var j = 0; j < selected.length; j++) {
        var cells = selected[j].querySelectorAll('td');
        var row = [];
        for (var k = 0; k < cells.length; k++) {
            row.push(cells[k].textContent.trim());
        }
        lines.push(row.join('\t'));
    }

    var tsv = lines.join('\n');
    var count = selected.length;

    // コピー実行
    doCopy(tsv, count);
}

/**
 * クリップボードにコピー（複数手法でフォールバック）
 */
function doCopy(text, rowCount) {
    // 方法1: Clipboard API (HTTPS環境のみ)
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
            showBottomBar(rowCount + ' rows copied to clipboard', 'success');
        }).catch(function() {
            doCopyFallback(text, rowCount);
        });
        return;
    }

    // 方法2: execCommand fallback
    doCopyFallback(text, rowCount);
}

function doCopyFallback(text, rowCount) {
    var ta = document.createElement('textarea');
    ta.value = text;
    // 画面内に配置（offscreenだとコピーできないブラウザがある）
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.width = '1px';
    ta.style.height = '1px';
    ta.style.opacity = '0.01';
    ta.style.zIndex = '-1';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();

    var success = false;
    try {
        success = document.execCommand('copy');
    } catch (e) {
        success = false;
    }
    document.body.removeChild(ta);

    if (success) {
        showBottomBar(rowCount + ' rows copied to clipboard', 'success');
    } else {
        // 完全に失敗した場合: モーダルでテキストを表示してユーザーに手動コピーさせる
        showCopyModal(text, rowCount);
    }
}

/**
 * コピー失敗時のモーダル表示
 */
function showCopyModal(text, rowCount) {
    // 既存モーダルがあれば削除
    var old = document.getElementById('copy-modal-overlay');
    if (old) old.remove();

    var overlay = document.createElement('div');
    overlay.id = 'copy-modal-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;';

    var modal = document.createElement('div');
    modal.style.cssText = 'background:#fff;border-radius:8px;padding:20px;width:80%;max-width:700px;max-height:80vh;display:flex;flex-direction:column;';

    var title = document.createElement('div');
    title.style.cssText = 'font-weight:bold;margin-bottom:10px;font-size:16px;';
    title.textContent = rowCount + ' rows — Ctrl+C to copy, then close';

    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.cssText = 'width:100%;height:300px;font-family:monospace;font-size:12px;margin-bottom:10px;';
    textarea.readOnly = true;

    var closeBtn = document.createElement('button');
    closeBtn.textContent = 'Close';
    closeBtn.style.cssText = 'align-self:flex-end;padding:6px 20px;background:#6c757d;color:#fff;border:none;border-radius:4px;cursor:pointer;';
    closeBtn.onclick = function() { overlay.remove(); };

    modal.appendChild(title);
    modal.appendChild(textarea);
    modal.appendChild(closeBtn);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // テキスト全選択
    textarea.focus();
    textarea.select();
}

/**
 * CSVエクスポート
 */
function exportCSV() {
    var params = buildParams();
    window.location.href = '/api/export_csv?' + params.toString();
}

/**
 * 画面下部の通知バー
 */
function showBottomBar(message, type) {
    var bar = document.getElementById('kpi-bottom-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'kpi-bottom-bar';
        bar.style.cssText = 'position:fixed;bottom:0;left:0;width:100%;z-index:9999;text-align:center;padding:12px 20px;font-size:15px;font-weight:bold;transition:transform 0.3s ease;transform:translateY(100%);';
        document.body.appendChild(bar);
    }

    if (type === 'success') {
        bar.style.background = '#198754';
        bar.style.color = '#fff';
    } else if (type === 'warning') {
        bar.style.background = '#ffc107';
        bar.style.color = '#000';
    } else {
        bar.style.background = '#333';
        bar.style.color = '#fff';
    }

    bar.textContent = message;
    // スライドイン
    bar.style.transform = 'translateY(0)';
    // 3秒後にスライドアウト
    clearTimeout(bar._timer);
    bar._timer = setTimeout(function() {
        bar.style.transform = 'translateY(100%)';
    }, 3000);
}

// Ctrl+C でコピー
document.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        var table = document.getElementById('kpi-table');
        if (table && table.querySelectorAll('tbody tr.selected').length > 0) {
            e.preventDefault();
            copySelectedRows();
        }
    }
});

// Ctrl+R でリロード
document.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
        e.preventDefault();
        reloadKPI();
    }
});
