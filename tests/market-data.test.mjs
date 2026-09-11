import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeBinance,normalizeTwelve,fetchMarket} from '../dist/market-data.mjs';
const kline=(i)=>[1700000000000+i*3600000,'10','12','9','11','5',1700000000000+(i+1)*3600000-1];
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
test('Binance removes open candles using exchange time and sorts data',()=>{
 const rows=normalizeBinance([kline(2),kline(1),kline(0)],1700000000000+2*3600000);
 assert.equal(rows.length,2);assert.equal(rows[0].open,10);assert.equal(rows[0].time,1700000000);
});
test('Twelve Data sorts UTC forex candles, omits newest and supplies missing volume',()=>{
 const data={values:['2025-01-03 10:00:00','2025-01-01 10:00:00','2025-01-02 10:00:00'].map(datetime=>({datetime,open:'1.1',high:'1.2',low:'1.0',close:'1.15'}))};
 const rows=normalizeTwelve(data);assert.equal(rows.length,2);assert.equal(rows[0].time,Date.parse('2025-01-01T10:00:00Z')/1000);assert.equal(rows[1].volume,0);
});
test('invalid provider OHLC and duplicate timestamps are rejected',()=>{
 assert.throws(()=>normalizeBinance([kline(0),kline(0)],2e12));
 const bad=kline(1);bad[2]='8';assert.throws(()=>normalizeBinance([kline(0),bad],2e12));
});
test('historical crypto pagination is bounded and endTime does not overlap',async()=>{
 const rows=Array.from({length:1101},(_,i)=>kline(i));let calls=[];
 const fetcher=async(url,options)=>{assert.equal(options.credentials,'omit');calls.push(url);
 if(url.pathname.endsWith('/time'))return response({serverTime:2e12});
 const end=Number(url.searchParams.get('endTime')||2e12),limit=Number(url.searchParams.get('limit'));
 return response(rows.filter(r=>r[0]<=end).slice(-limit));};
 const data=await fetchMarket({market:'crypto',symbol:'BTC/USDT',interval:'1h',count:1100},fetcher);
 assert.equal(data.bars.length,1100);assert.equal(calls.length,3);assert.equal(calls[2].searchParams.get('endTime'),String(rows[101][0]-1));assert.equal(data.marketMeta.quoteCurrency,'USDT');assert.equal(data.bars[0].time,rows[1][0]/1000);
});
test('forex and commodity requests require keys and use official time-series arguments',async()=>{
 await assert.rejects(fetchMarket({market:'forex',symbol:'EUR/USD',count:100}),/API key/);
 for(const [market,symbol] of [['forex','EUR/CZK'],['commodities','XAU/USD']]){
 const out=await fetchMarket({market,symbol,count:5000,interval:'1d',apiKey:'test-only-key'},async(url)=>{
 assert.equal(url.hostname,'api.twelvedata.com');assert.equal(url.searchParams.get('symbol'),symbol);assert.equal(url.searchParams.get('interval'),'1day');assert.equal(url.searchParams.get('timezone'),'UTC');assert.equal(url.searchParams.get('outputsize'),'5000');
 return response({meta:{symbol},values:['2025-01-01','2025-01-02','2025-01-03'].map(datetime=>({datetime,open:10,high:12,low:9,close:11}))});});
 assert.equal(out.bars.length,2);assert.equal(JSON.stringify(out).includes('test-only-key'),false);assert.equal(out.marketMeta.quoteCurrency,symbol.split('/')[1]);}
});
test('rate and auth errors are actionable and never include raw provider secrets',async()=>{
 await assert.rejects(fetchMarket({market:'forex',symbol:'EUR/USD',count:100,apiKey:'secret'},async()=>response({status:'error',code:429,message:'secret'})),/rate limit/);
 await assert.rejects(fetchMarket({market:'forex',symbol:'EUR/USD',count:100,apiKey:'secret'},async()=>response({status:'error',code:401,message:'secret'})),e=>e.message.includes('access denied')&&!e.message.includes('secret'));
});
