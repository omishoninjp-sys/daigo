// ============================================================
  // 設定
  // ============================================================
  const DAIKO_API_BASE = window.DAIKO_CONFIG.api_base;
  const DAIKO_API_KEY  = window.DAIKO_CONFIG.api_key;

  let currentProduct = null;
  let currentPricing = null;
  let currentVariants = [];
  let selectedColor = '';
  let selectedSize = '';

  // 定價費率表（和後端 config.py 一致）
  const PRICING_TIERS = [
    { min: 0,      max: 5000,      rate: 1.25 },
    { min: 5001,   max: 10000,     rate: 1.22 },
    { min: 10001,  max: 20000,     rate: 1.20 },
    { min: 20001,  max: 30000,     rate: 1.18 },
    { min: 30001,  max: 999999999, rate: 1.15 },
  ];
  const MIN_SERVICE_FEE = 300;
  const TWD_RATE = 0.21;

  // ============================================================
  // 前端定價計算
  // ============================================================
  function calcPrice(originalJpy) {
    let rate = 1.30;
    for (const tier of PRICING_TIERS) {
      if (originalJpy >= tier.min && originalJpy <= tier.max) {
        rate = tier.rate;
        break;
      }
    }
    let fee = Math.floor(originalJpy * (rate - 1));
    if (fee < MIN_SERVICE_FEE) fee = MIN_SERVICE_FEE;
    const selling = originalJpy + fee;
    const twd = Math.floor(selling * TWD_RATE);
    return { original: originalJpy, selling, twd, rate };
  }

  // ============================================================
  // 排隊狀態提示
  // ============================================================
  function showQueueStatus(msg, detail) {
    const el = document.getElementById('daiko-queue-status');
    document.getElementById('daiko-queue-msg').textContent = msg || '正在查詢商品資訊...';
    document.getElementById('daiko-queue-detail').textContent = detail || '';
    el.style.display = 'flex';
  }

  function hideQueueStatus() {
    document.getElementById('daiko-queue-status').style.display = 'none';
  }

  // ============================================================
  // Step 1: 查詢商品
  // ============================================================
  async function daikoSearch() {
    const urlInput = document.getElementById('daiko-url');
    const url = urlInput.value.trim();

    if (!url) { showError('請貼上商品連結'); return; }
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      showError('請輸入完整的網址（以 http:// 或 https:// 開頭）');
      return;
    }

    setLoading('daiko-search-btn', true);
    hideError();
    showQueueStatus('正在查詢商品資訊...', '請稍候，這可能需要幾秒鐘');

    try {
      const resp = await fetch(`${DAIKO_API_BASE}/api/scrape`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': DAIKO_API_KEY,
        },
        body: JSON.stringify({ url }),
      });

      // 503 = 排隊超時
      if (resp.status === 503) {
        const errData = await resp.json().catch(() => ({}));
        showError(errData.detail || '目前查詢人數較多，請稍後再試');
        return;
      }

      const data = await resp.json();

      // ★ 硬擋品類／封鎖網站：後端已經判定這條連結不承接。
      //   一定要在下面那個 fallback 之前 —— 舊版直接切手動表單，
      //   等於讓客人自己把被擋下來的商品建成訂單，而且四則說明訊息
      //   （寶可夢卡牌／BEYBLADE／一番賞／抽選販售）從來沒顯示過。
      //   2026-09-07 實測：線上 daigo.js 全檔沒有 blocked 這個字。
      if (data.blocked) {
        showError(data.error || '這個連結目前不開放代購。', data.handoff_url);
        return;
      }

      if (!data.success || !data.product || !data.product.title) {
        showManualForm(url);
        return;
      }

      currentProduct = data.product;
      currentPricing = data.pricing;
      showPreview();

    } catch (err) {
      if (err.name === 'TypeError' && err.message.includes('Failed to fetch')) {
        showError('無法連線到伺服器，請稍後再試');
      } else {
        showManualForm(url);
      }
      console.error('Daiko search error:', err);
    } finally {
      setLoading('daiko-search-btn', false);
      hideQueueStatus();
    }
  }

  // ============================================================
  // Step 1b: 手動輸入表單
  // ============================================================
  function showManualForm(url) {
    document.getElementById('manual-url-display').textContent = url;
    document.getElementById('step-input').style.display = 'none';
    document.getElementById('step-manual').style.display = 'block';

    const priceInput = document.getElementById('manual-price');
    priceInput.addEventListener('input', () => {
      const val = parseInt(priceInput.value);
      const previewBox = document.getElementById('manual-price-preview');
      if (val > 0) {
        const p = calcPrice(val);
        const elMO = document.getElementById('manual-original-price');
        if (elMO) elMO.textContent = `¥${p.original.toLocaleString()}`;
        const elMS = document.getElementById('manual-selling-price');
        if (elMS) elMS.textContent = `¥${p.selling.toLocaleString()}`;
        const elMT = document.getElementById('manual-twd-price');
        if (elMT) elMT.textContent = `≈ NT$${p.twd.toLocaleString()}`;
        previewBox.style.display = 'block';
      } else {
        previewBox.style.display = 'none';
      }
    });
  }

  // ============================================================
  // 手動下單
  // ============================================================
  async function daikoManualOrder() {
    const title = document.getElementById('manual-title').value.trim();
    const priceStr = document.getElementById('manual-price').value.trim();
    const imageUrl = document.getElementById('manual-image').value.trim();
    const note = document.getElementById('manual-note').value.trim();
    const sourceUrl = document.getElementById('manual-url-display').textContent;

    if (!title) { alert('請填寫商品名稱'); return; }
    if (!priceStr || parseInt(priceStr) <= 0) { alert('請填寫正確的日幣價格'); return; }

    const originalPrice = parseInt(priceStr);
    const pricing = calcPrice(originalPrice);

    setLoading('daiko-manual-btn', true);

    try {
      const resp = await fetch(`${DAIKO_API_BASE}/api/create-manual`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': DAIKO_API_KEY,
        },
        body: JSON.stringify({
          title: note ? `${title}（${note}）` : title,
          price_jpy: pricing.selling,
          original_price_jpy: originalPrice,
          image_url: imageUrl,
          source_url: sourceUrl,
        }),
      });

      const data = await resp.json();
      // 手動表單是攔截的第二道（後端會用客人自己打的標題＋原網址再判一次）。
      // 被擋下來要給完整說明，不能用 alert 塞四行字。
      if (data.blocked) {
        backToInput();
        showError(data.error || '這個連結目前不開放代購。', data.handoff_url);
        return;
      }
      if (!data.success) { alert(data.error || '建立商品失敗'); return; }

      document.getElementById('daiko-checkout-link').href = data.checkout_url;
      document.getElementById('step-manual').style.display = 'none';
      document.getElementById('step-done').style.display = 'block';

    } catch (err) {
      alert('建立商品失敗，請稍後再試');
      console.error('Manual order error:', err);
    } finally {
      setLoading('daiko-manual-btn', false);
    }
  }

  // ============================================================
  // Step 2: 確認下單（自動抓取成功的流程）
  // ============================================================
  async function daikoCreateOrder() {
    if (!currentProduct) return;

    // ── 高單價提醒（方案 B：建議聯繫客服，但允許下單）──
    if (currentPricing && currentPricing.original_price_jpy > 100000) {
      const ok = confirm(
        '⚠️ 此商品日幣售價超過 ¥100,000\n\n' +
        '建議先透過 LINE @544kaytb 聯繫客服確認，再行下單。\n\n' +
        '如您已聯繫過客服並確認可代購，請點「確定」繼續下單。\n' +
        '如尚未聯繫客服，請點「取消」先聯繫客服。'
      );
      if (!ok) return;
    }
    // ─────────────────────────────────────────────────────────────

    // 檢查是否需要選 variant
    if (currentVariants.length > 0) {
      const hasColors = currentVariants.some(v => v.color);
      const hasSizes = currentVariants.some(v => v.size);

      if (hasColors && !selectedColor) { alert('請選擇顏色'); return; }
      if (hasSizes && !selectedSize) { alert('請選擇尺寸'); return; }

      const matched = currentVariants.find(v =>
        (!hasColors || v.color === selectedColor) &&
        (!hasSizes || v.size === selectedSize)
      );
      // 缺貨仍允許送出，讓店家收到通知後主動聯繫客人
    }

    setLoading('daiko-order-btn', true);

    try {
      const variantLabel = getSelectedVariantLabel();
      const titleOverride = variantLabel
        ? `${currentProduct.title}【${variantLabel}】`
        : undefined;

      const body = { url: currentProduct.source_url };
      if (titleOverride) body.title_override = titleOverride;

      const resp = await fetch(`${DAIKO_API_BASE}/api/create-order`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': DAIKO_API_KEY,
        },
        body: JSON.stringify(body),
      });

      if (resp.status === 503) {
        const errData = await resp.json().catch(() => ({}));
        showError(errData.detail || '目前人數較多，請稍後再試');
        return;
      }

      const data = await resp.json();
      // ★ #daiko-error 在 step-input 裡，而這一步 step-input 是隱藏的 ——
      //   不先切回去的話，錯誤訊息會顯示在一個看不見的容器裡。
      if (!data.success) {
        backToInput();
        showError(data.error || '建立商品失敗', data.handoff_url);
        return;
      }

      document.getElementById('daiko-checkout-link').href = data.checkout_url;
      document.getElementById('step-preview').style.display = 'none';
      document.getElementById('step-done').style.display = 'block';

    } catch (err) {
      showError('建立商品失敗，請稍後再試');
      console.error('Daiko order error:', err);
    } finally {
      setLoading('daiko-order-btn', false);
    }
  }

  // ============================================================
  // Variant 選擇器
  // ============================================================
  function renderVariants(variants) {
    currentVariants = variants || [];
    const selector = document.getElementById('variant-selector');
    const colorWrap = document.getElementById('variant-color-wrap');
    const sizeWrap = document.getElementById('variant-size-wrap');
    const colorOptions = document.getElementById('variant-color-options');
    const sizeOptions = document.getElementById('variant-size-options');

    selectedColor = '';
    selectedSize = '';
    colorOptions.innerHTML = '';
    sizeOptions.innerHTML = '';

    if (!currentVariants.length) { selector.style.display = 'none'; return; }
    selector.style.display = 'block';

    const colors = [...new Set(currentVariants.map(v => v.color).filter(Boolean))];
    const sizes  = [...new Set(currentVariants.map(v => v.size).filter(Boolean))];

    if (colors.length > 0) {
      colorWrap.style.display = 'block';
      colors.forEach(color => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'daiko-variant-btn';
        btn.textContent = color;
        btn.setAttribute('data-color', color);
        const hasStock = currentVariants.some(v => v.color === color && v.in_stock);
        if (!hasStock) btn.classList.add('out-of-stock');
        btn.addEventListener('click', () => {
          selectedColor = color;
          colorOptions.querySelectorAll('.daiko-variant-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          if (sizes.length > 0) updateSizeAvailability();
          updateVariantImage();
          updateStockStatus();
          updateVariantPrice();
        });
        colorOptions.appendChild(btn);
      });
      const firstInStock = colors.find(c => currentVariants.some(v => v.color === c && v.in_stock));
      const firstColor = firstInStock || colors[0];
      if (firstColor) {
        selectedColor = firstColor;
        const firstColorBtn = [...colorOptions.querySelectorAll('.daiko-variant-btn')].find(b => b.getAttribute('data-color') === firstColor);
        if (firstColorBtn) firstColorBtn.classList.add('active');
      }
    } else {
      colorWrap.style.display = 'none';
    }

    if (sizes.length > 0) {
      sizeWrap.style.display = 'block';
      sizes.forEach(size => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'daiko-variant-btn';
        btn.textContent = size;
        btn.setAttribute('data-size', size);
        btn.addEventListener('click', () => {
          selectedSize = size;
          sizeOptions.querySelectorAll('.daiko-variant-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          updateStockStatus();
          updateVariantPrice();
        });
        sizeOptions.appendChild(btn);
      });
      updateSizeAvailability();
      const availableSizes = getAvailableSizes();
      const firstSize = availableSizes.length > 0 ? availableSizes[0] : sizes[0];
      if (firstSize) {
        selectedSize = firstSize;
        const firstSizeBtn = [...sizeOptions.querySelectorAll('.daiko-variant-btn')].find(b => b.getAttribute('data-size') === firstSize);
        if (firstSizeBtn) firstSizeBtn.classList.add('active');
      }
    } else {
      sizeWrap.style.display = 'none';
    }

    updateStockStatus();
    updateVariantImage();
    updateVariantPrice();
  }

  function getAvailableSizes() {
    if (!selectedColor) {
      return [...new Set(currentVariants.filter(v => v.in_stock).map(v => v.size).filter(Boolean))];
    }
    return [...new Set(
      currentVariants.filter(v => v.color === selectedColor && v.in_stock).map(v => v.size).filter(Boolean)
    )];
  }

  function updateSizeAvailability() {
    const sizeOptions = document.getElementById('variant-size-options');
    const availableSizes = getAvailableSizes();
    sizeOptions.querySelectorAll('.daiko-variant-btn').forEach(btn => {
      const size = btn.getAttribute('data-size');
      if (availableSizes.includes(size)) {
        btn.classList.remove('out-of-stock');
      } else {
        btn.classList.add('out-of-stock');
        if (selectedSize === size) { btn.classList.remove('active'); selectedSize = ''; }
      }
    });
    if (!selectedSize || !availableSizes.includes(selectedSize)) {
      if (availableSizes.length > 0) {
        selectedSize = availableSizes[0];
        const newBtn = sizeOptions.querySelector(`[data-size="${availableSizes[0]}"]`);
        if (newBtn) newBtn.classList.add('active');
      }
    }
  }

  function updateVariantImage() {
    if (!selectedColor) return;
    const variant = currentVariants.find(v => v.color === selectedColor && v.image);
    if (variant && variant.image) {
      document.getElementById('preview-image').src = variant.image;
    }
  }

  function updateStockStatus() {
    const statusEl = document.getElementById('variant-stock-status');
    const hasColors = currentVariants.some(v => v.color);
    const hasSizes  = currentVariants.some(v => v.size);
    if (hasColors && !selectedColor) { statusEl.textContent = '請選擇顏色'; statusEl.className = 'daiko-variant-stock'; return; }
    if (hasSizes  && !selectedSize)  { statusEl.textContent = '請選擇尺寸'; statusEl.className = 'daiko-variant-stock'; return; }
    const matched = currentVariants.find(v =>
      (!hasColors || v.color === selectedColor) && (!hasSizes || v.size === selectedSize)
    );
    if (matched && matched.in_stock) {
      statusEl.textContent = '✓ 有庫存'; statusEl.className = 'daiko-variant-stock in-stock';
    } else {
      statusEl.textContent = '✕ 已售完'; statusEl.className = 'daiko-variant-stock sold-out';
    }
  }

  function getSelectedVariantLabel() {
    const parts = [];
    if (selectedColor) parts.push(selectedColor);
    if (selectedSize)  parts.push(selectedSize);
    return parts.join(' / ');
  }

  // 根據選擇的 variant 更新顯示價格（用於不同 variant 價格不同的情況，如 nijisanji 套裝）
  function updateVariantPrice() {
    if (!currentVariants.length) return;

    const hasColors = currentVariants.some(v => v.color);
    const hasSizes  = currentVariants.some(v => v.size);

    // 找到目前選中的 variant
    const matched = currentVariants.find(v =>
      (!hasColors || !selectedColor || v.color === selectedColor) &&
      (!hasSizes  || !selectedSize  || v.size  === selectedSize)
    );

    if (!matched) return;

    // 只有當 variant 有自己的 price 且不同於預設時才更新
    const variantPrice = matched.price;
    if (!variantPrice || variantPrice <= 0) return;

    const p = calcPrice(variantPrice);
    const _elO = document.getElementById('preview-original-price');
    if (_elO) _elO.textContent = `¥${p.original.toLocaleString()}`;
    const _elS = document.getElementById('preview-selling-price');
    if (_elS) _elS.textContent = `¥${p.selling.toLocaleString()}`;
    const _elT = document.getElementById('preview-twd-price');
    if (_elT) _elT.textContent = `≈ NT$${p.twd.toLocaleString()}`;
  }

  // ============================================================
  // UI helpers
  // ============================================================
  function showPreview() {
    const p = currentProduct;
    const pr = currentPricing;

    document.getElementById('preview-title').textContent = p.title || '商品名稱';
    document.getElementById('preview-brand').textContent = p.brand || '';
    if (p.image_url) document.getElementById('preview-image').src = p.image_url;

    if (pr) {
      const elOrig = document.getElementById('preview-original-price');
      if (elOrig) elOrig.textContent = `¥${pr.original_price_jpy.toLocaleString()}`;
      const elSell = document.getElementById('preview-selling-price');
      if (elSell) elSell.textContent = `¥${pr.selling_price_jpy.toLocaleString()}`;
      if (pr.reference_price_twd) {
        const elTwd = document.getElementById('preview-twd-price');
        if (elTwd) elTwd.textContent = `≈ NT$${pr.reference_price_twd.toLocaleString()}`;
      }
    } else {
      const elOrig = document.getElementById('preview-original-price');
      if (elOrig) elOrig.textContent = '價格需另行報價';
      const elSell = document.getElementById('preview-selling-price');
      if (elSell) elSell.textContent = '聯繫客服';
    }

    renderVariants(p.variants);

    document.getElementById('step-input').style.display = 'none';
    document.getElementById('step-preview').style.display = 'block';
  }

  function daikoReset() {
    currentProduct = null;
    currentPricing = null;
    currentVariants = [];
    selectedColor = '';
    selectedSize = '';
    document.getElementById('daiko-url').value = '';
    document.getElementById('step-input').style.display = 'block';
    document.getElementById('step-preview').style.display = 'none';
    document.getElementById('step-manual').style.display = 'none';
    document.getElementById('step-done').style.display = 'none';
    document.getElementById('variant-selector').style.display = 'none';
    hideError();
    hideQueueStatus();
  }

  // ★ handoffUrl 是後端選填的「人工接手」深連結（例如帶好商品網址的 LINE 對話框）。
  //   後端還沒開始送這個欄位時傳進來是 undefined，行為與改動前完全相同。
  //
  // 🔴 連結一律用 createElement + textContent 建，**絕對不可以用 innerHTML** ——
  //    msg 與 handoffUrl 都是後端回來的字串，innerHTML 等於開一個 XSS 面。
  // 🔴 href 只收 https:。就算後端哪天填錯或被塞了 javascript: / data:，
  //    這裡也不會變成可點的注入點。
  function showError(msg, handoffUrl) {
    const el = document.getElementById('daiko-error');
    el.textContent = msg;           // 指派 textContent 會一併清掉上次附加的連結
    if (handoffUrl) {
      let safe = null;
      try {
        const u = new URL(handoffUrl, window.location.origin);
        if (u.protocol === 'https:') safe = u.href;
      } catch (e) { safe = null; }
      if (safe) {
        const a = document.createElement('a');
        a.href = safe;
        a.target = '_blank';
        a.rel = 'noopener';
        a.className = 'daiko-error-handoff';
        a.textContent = '用 LINE 幫我處理這件商品 →';
        el.appendChild(document.createElement('br'));
        el.appendChild(a);
      }
    }
    el.style.display = 'block';
    // 🔴 #daiko-error 在 step-input 的最底部，離輸入框約 1,400px ——
    //    2026-09-07 實機測試：訊息有顯示，但客人按完「立即查詢」畫面毫無變化，
    //    要往下捲很久才看得到，等於沒顯示。一定要主動捲過去。
    try {
      const reduce = window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    } catch (e) {
      el.scrollIntoView();
    }
  }

  // 硬擋／建單失敗時回到輸入步驟，讓 #daiko-error 看得見（它在 step-input 裡面）。
  // 刻意不清掉輸入框的網址：訊息常常是「請改貼二手平台的連結」，
  // 客人可能還要複製原網址去別的站搜。
  function backToInput() {
    document.getElementById('step-input').style.display = 'block';
    document.getElementById('step-preview').style.display = 'none';
    document.getElementById('step-manual').style.display = 'none';
  }
  function hideError() { document.getElementById('daiko-error').style.display = 'none'; }
  function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    btn.disabled = loading;
    btn.querySelector('.btn-text').style.display = loading ? 'none' : 'inline';
    btn.querySelector('.btn-loading').style.display = loading ? 'inline' : 'none';
  }


  // ============================================================
  // 費率表 & 運費表：依訪客貨幣顯示本地參考金額
  // 支援 TWD / HKD / USD / CAD，JPY 不顯示
  // ============================================================
  (async function initLocalCurrencyTable() {

    // 用國家代碼判斷顯示貨幣（currency.active 永遠是 JPY 無法用）
    const COUNTRY_MAP = {
      TW: { code: 'TWD', sym: 'NT$',  fallback: 0.211  },
      HK: { code: 'HKD', sym: 'HK$',  fallback: 0.052  },
      US: { code: 'USD', sym: 'US$',  fallback: 0.0067 },
      CA: { code: 'CAD', sym: 'CA$',  fallback: 0.0092 },
    };

    const country = (window.Shopify && window.Shopify.country)
      ? window.Shopify.country.toUpperCase()
      : 'TW';

    const config = COUNTRY_MAP[country] || COUNTRY_MAP['TW'];
    const activeCurrency = config.code;

    const sym = config.sym;
    let rate = config.fallback;

    // 從 exchangerate-api.com 取即時匯率（1 小時快取）
    try {
      const cacheKey = `daiko_jpy_${activeCurrency}_rate`;
      const cacheTs  = `daiko_jpy_${activeCurrency}_ts`;
      const cached   = sessionStorage.getItem(cacheKey);
      const cachedTs = parseInt(sessionStorage.getItem(cacheTs) || '0');
      if (cached && (Date.now() - cachedTs < 3600000)) {
        rate = parseFloat(cached);
      } else {
        const res  = await fetch('https://api.exchangerate-api.com/v4/latest/JPY');
        const json = await res.json();
        if (json.rates && json.rates[activeCurrency]) {
          rate = json.rates[activeCurrency];
          sessionStorage.setItem(cacheKey, rate);
          sessionStorage.setItem(cacheTs, Date.now());
        }
      }
    } catch(e) {
      console.warn('[daiko] 匯率 API 失敗，使用備用匯率:', rate);
    }

    function fmt(val) { return `${sym}${val.toLocaleString()}`; }

    // 費率表
    const feeHeader = document.getElementById('fee-local-header');
    if (feeHeader) {
      feeHeader.textContent = `參考${sym}`;
      feeHeader.style.display = 'table-cell';
    }
    document.querySelectorAll('.daiko-local-fee').forEach(td => {
      const minJpy = parseInt(td.getAttribute('data-jpy-min') || '0');
      const maxJpy = td.getAttribute('data-jpy-max');
      const localMin = Math.round(calcPrice(minJpy).selling * rate);
      const suffix = ' <span style="color:var(--dk-green);font-weight:600;">+運費</span>';
      if (maxJpy === '' || maxJpy === null) {
        td.innerHTML = `≈ ${fmt(localMin)}+${suffix}`;
      } else {
        const localMax = Math.round(calcPrice(parseInt(maxJpy)).selling * rate);
        td.innerHTML = `${fmt(localMin)} ~ ${fmt(localMax)}${suffix}`;
      }
      td.style.display = 'table-cell';
    });

    // 運費表
    const shipHeader = document.getElementById('ship-local-header');
    if (shipHeader) {
      shipHeader.textContent = `參考${sym}`;
      shipHeader.style.display = 'table-cell';
    }
    document.querySelectorAll('.daiko-local-ship').forEach(td => {
      const jpy = parseInt(td.getAttribute('data-jpy') || '0');
      if (!jpy) return;
      const prefix = td.textContent;
      const local = Math.round(jpy * rate);
      td.textContent = (prefix === '+' ? '+ ' : '') + `≈ ${fmt(local)}`;
      td.style.display = 'table-cell';
    });
  })();

  document.getElementById('daiko-url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') daikoSearch();
  });

  // 任何方式輸入 URL 後自動查詢（Ctrl+V、手機長按貼上等）
  let _autoSearchTimer = null;
  document.getElementById('daiko-url').addEventListener('input', () => {
    clearTimeout(_autoSearchTimer);
    _autoSearchTimer = setTimeout(() => {
      const url = document.getElementById('daiko-url').value.trim();
      if (url.startsWith('http://') || url.startsWith('https://')) {
        daikoSearch();
      }
    }, 300);
  });

  // FAQ 開關
  function daikoToggleFaq(el) {
    el.classList.toggle('open');
  }

  // ── 貼上按鈕 ──
  async function daikoPasteFromClipboard() {
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim().startsWith('http')) {
        document.getElementById('daiko-url').value = text.trim();
        document.getElementById('daiko-clipboard-hint').style.display = 'none';
        // 程式碼設值不會觸發 input 事件，需手動呼叫
        setTimeout(() => daikoSearch(), 150);
        // 手機大按鈕：貼上後改成查詢中狀態
        const mobileBtn = document.getElementById('daiko-mobile-paste');
        if (mobileBtn && window.innerWidth <= 640) {
          mobileBtn.style.background = 'linear-gradient(135deg, #2e7d32 0%, #1b5e20 100%)';
          mobileBtn.querySelector('.daiko-mobile-paste-text strong').textContent = '✓ 連結已填入！';
          mobileBtn.querySelector('.daiko-mobile-paste-text small').textContent = '按下方「立即查詢」繼續';
          mobileBtn.querySelector('.daiko-mobile-paste-icon').textContent = '✅';
          // 自動聚焦到查詢按鈕
          setTimeout(() => document.getElementById('daiko-search-btn').scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 300);
        }
        // 電腦版小按鈕反饋
        const btn = document.getElementById('daiko-paste-btn');
        if (btn) {
          btn.textContent = '✓ 已貼上';
          setTimeout(() => {
            btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> 貼上';
          }, 1500);
        }
      } else {
        alert('剪貼簿沒有偵測到商品連結，請先從商品頁複製網址再回來！');
      }
    } catch (e) {
      // 瀏覽器不允許時（罕見），讓使用者手動貼
      const input = document.getElementById('daiko-url');
      input.focus();
      try { document.execCommand('paste'); } catch(e2) {}
      alert('請長按輸入框選擇「貼上」');
    }
  }

  // ── 自動偵測剪貼簿 ──
  let _clipboardChecked = false;
  async function daikoCheckClipboard() {
    if (_clipboardChecked) return;
    _clipboardChecked = true;
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim().startsWith('http') && text.length < 500) {
        const url = text.trim();
        const hint = document.getElementById('daiko-clipboard-hint');
        const label = document.getElementById('daiko-clipboard-text');
        if (hint && label) {
          // 顯示縮短的 URL
          const display = url.length > 40 ? url.substring(0, 38) + '…' : url;
          label.textContent = display;
          hint.style.display = 'flex';
          window._clipboardUrl = url;
        }
      }
    } catch (e) { /* 使用者未授權剪貼簿，靜默失敗 */ }
  }

  function daikoUseClipboard() {
    if (window._clipboardUrl) {
      document.getElementById('daiko-url').value = window._clipboardUrl;
      document.getElementById('daiko-clipboard-hint').style.display = 'none';
      setTimeout(() => daikoSearch(), 150);
    }
  }

  // 頁面取得焦點時偵測（使用者從其他 app 切回來）
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      _clipboardChecked = false;
      setTimeout(daikoCheckClipboard, 400);
    }
  });
  // 頁面載入也偵測一次
  setTimeout(daikoCheckClipboard, 800);