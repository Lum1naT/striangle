import {parseCSV} from './engine.mjs';

export const INTERVALS = {
  '1m': {label: '1 minute', twelve: '1min'},
  '5m': {label: '5 minutes', twelve: '5min'},
  '15m': {label: '15 minutes', twelve: '15min'},
  '1h': {label: '1 hour', twelve: '1h'},
  '4h': {label: '4 hours', twelve: '4h'},
  '1d': {label: '1 day', twelve: '1day'},
};
export const MARKETS = {
  crypto: [['BTC/USDT','Bitcoin'],['XRP/USDT','XRP'],['SOL/USDT','Solana'],['ETH/USDT','Ethereum']],
  forex: [['EUR/USD','Euro / US dollar'],['GBP/USD','Pound / US dollar'],['USD/JPY','US dollar / Yen'],['AUD/USD','Australian dollar / US dollar'],['USD/CHF','US dollar / Swiss franc'],['EUR/CZK','Euro / Czech koruna'],['USD/CZK','US dollar / Czech koruna']],
  commodities: [['XAU/USD','Gold spot'],['XAG/USD','Silver spot'],['XPT/USD','Platinum spot'],['XPD/USD','Palladium spot'],['WTI/USD','WTI crude oil spot'],['XBR/USD','Brent crude oil spot']],
};
const BINANCE = 'https://data-api.binance.vision/api/v3/';
const TWELVE = 'https://api.twelvedata.com/';

function checkedBars(rows) {
  if (rows.length < 2) throw Error('The provider returned fewer than two completed candles. Try a different timeframe or symbol.');
  return parseCSV('time,open,high,low,close,volume\n' + rows.map(b => [b.time,b.open,b.high,b.low,b.close,b.volume ?? 0].join(',')).join('\n'));
}
export function normalizeBinance(rows, serverTime) {
  if (!Array.isArray(rows)) throw Error('Binance returned an unexpected response.');
  const bars = rows.filter(r => Array.isArray(r) && Number(r[6]) < serverTime)
    .map(r => ({time:Number(r[0])/1000,open:r[1],high:r[2],low:r[3],close:r[4],volume:r[5]}));
  bars.sort((a,b)=>a.time-b.time);
  return checkedBars(bars);
}
export function normalizeTwelve(payload) {
  if (!Array.isArray(payload?.values)) throw Error('Twelve Data returned no candles. Check the symbol and your plan.');
  const rows = payload.values.map(v => ({
    time: Date.parse(v.datetime.length === 10 ? v.datetime+'T00:00:00Z' : v.datetime.replace(' ','T')+'Z')/1000,
    open:v.open,high:v.high,low:v.low,close:v.close,volume:v.volume ?? 0,
  })).sort((a,b)=>a.time-b.time);
  // Exclude the newest bar even when it appears complete: daily FX/commodity
  // session boundaries are provider-specific and must not be inferred from UTC.
  rows.pop();
  return checkedBars(rows);
}
async function requestJSON(url, signal, fetcher) {
  let response;
  try { response = await fetcher(url, {signal,cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer'}); }
  catch (e) {
    if (signal.aborted) throw Error('Request cancelled or timed out. Please try again.');
    throw Error('Cannot reach the provider. Check your connection; browser or regional restrictions may block this feed.');
  }
  let data;
  try { data = await response.json(); }
  catch { throw Error('The provider did not return market data. Please try again later.'); }
  if (!response.ok || data?.status === 'error' || (data?.code && Number(data.code) < 0)) {
    const code = Number(data?.code ?? response.status);
    if (code === 429 || response.status === 429 || response.status === 418) throw Error('Provider rate limit reached. Wait before fetching again.');
    if ([401,403].includes(code)) throw Error('Provider access denied. Check your API key and symbol entitlement.');
    if (code === -1121 || code === 400 || code === 404) throw Error('Symbol or interval unavailable. Check the provider symbol and your plan.');
    if (response.status === 451) throw Error('This provider is unavailable in your region.');
    throw Error('The provider could not fulfil this request. Check your key, symbol, plan and quota.');
  }
  return data;
}
export async function fetchMarket({market,symbol,interval='1h',count=1000,apiKey='',signal,onProgress}, fetcher=fetch) {
  if (!MARKETS[market] || !INTERVALS[interval]) throw Error('Choose a supported market and timeframe.');
  symbol = symbol.trim().toUpperCase();
  if (!/^[A-Z0-9]{1,15}\/[A-Z0-9]{2,10}$/.test(symbol)) throw Error('Use a pair such as BTC/USDT, EUR/USD or XAU/USD.');
  const maximum = market === 'crypto' ? 100000 : 5000;
  if (!Number.isInteger(count) || count<100 || count>maximum) throw Error(`Choose between 100 and ${maximum.toLocaleString('en-US')} candles for this market.`);
  if (market !== 'crypto' && !apiKey.trim()) throw Error('Enter your Twelve Data API key to fetch forex or commodities.');
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener('abort',abort,{once:true});
  const timer = setTimeout(abort,Math.max(45000,Math.ceil(count/1000)*5000));
  const get = url => requestJSON(url,controller.signal,fetcher);
  try {
    let bars,provider;
    if (market === 'crypto') {
      provider='Binance';
      const time = await get(new URL('time',BINANCE));
      if (!Number.isFinite(time.serverTime)) throw Error('Binance server time is unavailable.');
      const rows = new Map();
      let endTime=time.serverTime, completed=0, page=0;
      while(completed<count) {
        const url=new URL('klines',BINANCE);
        url.search=new URLSearchParams({symbol:symbol.replace('/',''),interval,limit:String(Math.min(1000,count+1-completed)),endTime:String(endTime)});
        const chunk=await get(url);
        if (!Array.isArray(chunk)) throw Error('Binance returned an unexpected response.');
        if (!chunk.length) break;
        const first=chunk.reduce((value,row)=>Math.min(value,Number(row[0])),Infinity);
        if (!Number.isFinite(first) || first>endTime) throw Error('Invalid historical pagination from Binance.');
        for(const row of chunk) {
          const at=Number(row[0]);
          if(at>endTime) throw Error('Binance returned candles outside the requested window.');
          if(!rows.has(at) && Number(row[6])<time.serverTime) completed++;
          rows.set(at,row);
        }
        endTime=first-1;
        onProgress?.({received:Math.min(completed,count),requested:count,pages:++page});
        if(completed<count) await new Promise((resolve,reject)=>{
          const cancel=()=>{clearTimeout(wait);controller.signal.removeEventListener('abort',cancel);reject(Error('Request cancelled or timed out. Please try again.'));};
          const wait=setTimeout(()=>{controller.signal.removeEventListener('abort',cancel);resolve();},100);
          controller.signal.addEventListener('abort',cancel,{once:true});
          if(controller.signal.aborted) cancel();
        });
      }
      bars=normalizeBinance([...rows.values()].filter(row=>Number(row[6])<time.serverTime).sort((a,b)=>Number(a[0])-Number(b[0])).slice(-count),time.serverTime);
    } else {
      provider='Twelve Data';
      const url=new URL('time_series',TWELVE);
      url.search=new URLSearchParams({symbol,interval:INTERVALS[interval].twelve,outputsize:String(Math.min(5000,count+1)),timezone:'UTC',order:'ASC',apikey:apiKey.trim()});
      const payload=await get(url);
      if (payload.meta?.symbol && payload.meta.symbol.toUpperCase()!==symbol) throw Error('Provider returned a different symbol; the data was not loaded.');
      bars=normalizeTwelve(payload).slice(-count);
    }
    return {bars,dataName:symbol,isSample:false,marketMeta:{market,provider,symbol,interval,requestedCount:count,quoteCurrency:symbol.split('/')[1],fetchedAt:Date.now(),lastCandle:bars.at(-1).time}};
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort',abort);
  }
}
