"""
搜尋需求情報儀表板（由後端同源提供，避免 file:// 的 CORS 問題）。
main.py 加一個路由回傳 INSIGHTS_HTML 即可：
    @app.get("/admin/insights")
    async def insights_page():
        from fastapi.responses import HTMLResponse
        from insights_page import INSIGHTS_HTML
        return HTMLResponse(content=INSIGHTS_HTML)
開 https://<你的daigo網址>/admin/insights ，填 API key → 載入。
（頁面外殼不含資料；/api/search-stats 仍需 x-api-key，網址外流也看不到資料。）
"""

INSIGHTS_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GOYOUTATI 搜尋需求情報</title>
<style>
  :root{ --navy:#1e2d5a; --gold:#f0a500; --bg:#f4f5f8; --line:#e4e6ec; --red:#c0392b; }
  *{ box-sizing:border-box; }
  body{ margin:0; font-family:"Noto Sans TC","Hiragino Sans",sans-serif; background:var(--bg); color:#222; }
  .wrap{ max-width:1000px; margin:0 auto; padding:24px 16px 60px; }
  h1{ color:var(--navy); font-size:22px; margin:0 0 4px; }
  .sub{ color:#777; font-size:13px; margin:0 0 18px; }
  .cfg{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; background:#fff; border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:18px; }
  .cfg input, .cfg select{ height:38px; border:1px solid #ccc; border-radius:7px; padding:0 10px; font-size:14px; }
  .cfg input.key{ flex:1; min-width:200px; }
  .cfg button{ height:38px; padding:0 20px; background:var(--navy); color:#fff; border:none; border-radius:7px; font-size:14px; font-weight:600; cursor:pointer; }
  .cards{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }
  .kpi{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .kpi .n{ font-size:26px; font-weight:700; color:var(--navy); }
  .kpi.warn .n{ color:var(--red); }
  .kpi .l{ font-size:12px; color:#777; margin-top:2px; }
  .panel{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:18px; }
  .panel h2{ font-size:15px; color:var(--navy); margin:0 0 12px; display:flex; align-items:center; gap:8px; }
  .tag{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px; }
  .tag.gold{ background:#fff4dc; color:#9a6b00; }
  .tag.red{ background:#fde8e6; color:var(--red); }
  table{ width:100%; border-collapse:collapse; font-size:13px; }
  th,td{ text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
  th{ color:#888; font-weight:600; font-size:12px; }
  td.num, th.num{ text-align:right; font-variant-numeric:tabular-nums; }
  .term{ font-weight:600; color:#222; }
  .zero-rate{ color:var(--red); font-weight:600; }
  .bars{ display:flex; align-items:flex-end; gap:3px; height:90px; padding-top:8px; }
  .bar{ flex:1; background:var(--navy); border-radius:3px 3px 0 0; min-height:2px; position:relative; }
  .bar span{ position:absolute; bottom:-18px; left:50%; transform:translateX(-50%); font-size:9px; color:#aaa; white-space:nowrap; }
  .muted{ color:#999; font-size:13px; padding:10px 0; }
  .recent{ font-size:12px; color:#555; }
  .recent .r{ display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px dashed var(--line); gap:10px; }
  .recent .z{ color:var(--red); }
  .err{ background:#fde8e6; color:var(--red); padding:10px 14px; border-radius:8px; font-size:13px; margin-bottom:14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>搜尋需求情報</h1>
  <p class="sub">客人在站內搜尋打了什麼字。重點看「零結果搜尋詞」——有人找、但你沒上架／沒貨的真實需求。</p>

  <div class="cfg">
    <input class="key" id="key" type="password" placeholder="API key（API_SECRET_KEY）">
    <select id="days">
      <option value="7">近 7 天</option>
      <option value="30" selected>近 30 天</option>
      <option value="90">近 90 天</option>
    </select>
    <button id="load">載入</button>
  </div>

  <div id="err" class="err" style="display:none;"></div>
  <div id="content" style="display:none;">
    <div class="cards">
      <div class="kpi"><div class="n" id="k-total">—</div><div class="l">搜尋次數</div></div>
      <div class="kpi"><div class="n" id="k-distinct">—</div><div class="l">不重複關鍵字</div></div>
      <div class="kpi warn"><div class="n" id="k-zero">—</div><div class="l">零結果搜尋次數</div></div>
    </div>

    <div class="panel">
      <h2>零結果搜尋詞 <span class="tag red">未滿足需求</span></h2>
      <div id="zero-wrap"></div>
    </div>

    <div class="panel">
      <h2>熱門搜尋詞 <span class="tag gold">需求熱度</span></h2>
      <div id="top-wrap"></div>
    </div>

    <div class="panel">
      <h2>每日搜尋量</h2>
      <div class="bars" id="daily"></div>
    </div>

    <div class="panel">
      <h2>最近搜尋</h2>
      <div class="recent" id="recent"></div>
    </div>
  </div>
</div>

<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  try { $('key').value = localStorage.getItem('gp_si_key') || ''; } catch(e){}

  function esc(s){ var d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }
  function showErr(t){ var e=$('err'); e.textContent=t; e.style.display=t?'block':'none'; }

  function load(){
    var key  = $('key').value.trim();
    var days = $('days').value;
    if(!key){ showErr('請填 API key'); return; }
    try { localStorage.setItem('gp_si_key', key); } catch(e){}
    showErr('');
    $('load').textContent = '載入中…'; $('load').disabled = true;

    // 同源相對路徑，無 CORS 問題
    fetch('/api/search-stats?days=' + days, { headers:{ 'x-api-key': key } })
      .then(function(r){ return r.json(); })
      .then(function(d){
        $('load').textContent = '載入'; $('load').disabled = false;
        if(!d || !d.success){ showErr((d && d.error) || '讀取失敗（API key 是否正確？）'); return; }
        if(!d.available){ showErr('資料庫尚未就緒，或還沒有任何搜尋紀錄。'); return; }
        render(d);
      })
      .catch(function(){
        $('load').textContent = '載入'; $('load').disabled = false;
        showErr('讀取失敗，請稍後再試。');
      });
  }

  function render(d){
    $('content').style.display = 'block';
    var t = d.totals || {};
    $('k-total').textContent = (t.searches||0).toLocaleString();
    $('k-distinct').textContent = (t.distinct_terms||0).toLocaleString();
    $('k-zero').textContent = (t.zero_result_searches||0).toLocaleString();

    var zero = d.zero_terms || [];
    if(!zero.length){
      $('zero-wrap').innerHTML = '<div class="muted">這段期間沒有零結果的搜尋 👍</div>';
    } else {
      var zh = '<table><thead><tr><th>關鍵字</th><th class="num">搜尋次數</th><th>最後一次</th></tr></thead><tbody>';
      zero.forEach(function(it){
        zh += '<tr><td class="term">'+esc(it.raw)+'</td><td class="num">'+it.count+'</td><td>'+esc((it.last_ts||'').slice(0,10))+'</td></tr>';
      });
      $('zero-wrap').innerHTML = zh + '</tbody></table>';
    }

    var top = d.top_terms || [];
    if(!top.length){
      $('top-wrap').innerHTML = '<div class="muted">尚無資料</div>';
    } else {
      var th = '<table><thead><tr><th>關鍵字</th><th class="num">搜尋次數</th><th class="num">平均結果數</th><th class="num">零結果率</th></tr></thead><tbody>';
      top.forEach(function(it){
        var zr = Math.round((it.zero_rate||0)*100);
        var zrCell = zr>0 ? '<span class="zero-rate">'+zr+'%</span>' : '0%';
        th += '<tr><td class="term">'+esc(it.raw)+'</td><td class="num">'+it.count+'</td><td class="num">'+(it.avg_results||0)+'</td><td class="num">'+zrCell+'</td></tr>';
      });
      $('top-wrap').innerHTML = th + '</tbody></table>';
    }

    var daily = d.daily || [];
    var max = daily.reduce(function(m,x){ return Math.max(m, x.count); }, 1);
    $('daily').innerHTML = daily.length
      ? daily.map(function(x){
          var h = Math.round((x.count/max)*100);
          return '<div class="bar" style="height:'+h+'%" title="'+x.date+'：'+x.count+'"><span>'+x.date.slice(5)+'</span></div>';
        }).join('')
      : '<div class="muted">尚無資料</div>';

    var recent = d.recent || [];
    $('recent').innerHTML = recent.length
      ? recent.map(function(r){
          var zc = r.result_count===0 ? 'z' : '';
          var trans = r.translated && r.translated!==r.raw ? ' → '+esc(r.translated) : '';
          return '<div class="r"><span class="'+zc+'">'+esc(r.raw)+trans+'</span>'+
                 '<span>'+esc(r.source||'')+'｜'+r.result_count+' 筆｜'+esc((r.ts||'').slice(0,16).replace('T',' '))+'</span></div>';
        }).join('')
      : '<div class="muted">尚無資料</div>';
  }

  $('load').addEventListener('click', load);
  $('key').addEventListener('keydown', function(e){ if(e.key==='Enter') load(); });
})();
</script>
</body>
</html>
"""
