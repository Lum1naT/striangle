import {backtest,validate} from './engine.mjs';
export const LIMIT=10000;
export const WORK_LIMIT=50000000;
export function parseValues(text, fallback){
  if(!text.trim())return [fallback];
  const out=[];
  for(const part of text.split(',')){
    const p=part.trim();
    if(!p)throw Error('Empty value in parameter list.');
    if(p.includes(':')){
      const terms=p.split(':').map(Number);
      if(terms.length!==3||!terms.every(Number.isFinite))throw Error('Ranges use start:end:step, for example 10:30:10.');
      const [a,b,step]=terms;
      if(step<=0||b<a||(b-a)/step>99)throw Error('Use ascending ranges with positive steps and at most 100 values.');
      for(let i=0;i<=Math.floor((b-a)/step+1e-9);i++)out.push(Number((a+i*step).toFixed(10)));
    }else{const n=Number(p);if(!Number.isFinite(n))throw Error('Enter numeric values separated by commas.');out.push(n);}
  }
  const unique=[...new Set(out)];if(unique.length>100)throw Error('At most 100 values per parameter.');return unique;
}
export function buildGrid(base,spec){
  validate(base);
  if(!spec.families?.length)throw Error('Select at least one strategy family.');
  const results=[];let attempted=0;
  for(const strategy of [...new Set(spec.families)]){
    if(!['cross','rsi','custom'].includes(strategy))throw Error('Unknown strategy family.');
    const operands=strategy==='custom'?[base.entry.left,base.entry.right,base.exit.left,base.exit.right]:[];
    const keys=['stop','take'];
    if(strategy==='cross'||operands.some(x=>x==='fast'||x==='slow'))keys.push('fast','slow');
    if(strategy==='rsi'||operands.includes('rsi'))keys.push('rsiPeriod');
    if(strategy==='rsi')keys.push('oversold','overbought');
    if(strategy==='custom'&&base.entry.right==='number')keys.push('entryValue');
    if(strategy==='custom'&&base.exit.right==='number')keys.push('exitValue');
    const values=keys.map(k=>parseValues(spec.ranges[k]||'',k==='entryValue'?base.entry.value:k==='exitValue'?base.exit.value:base[k]));
    const count=values.reduce((a,v)=>a*v.length,1);attempted+=count;
    if(attempted>LIMIT)throw Error(`Search exceeds ${LIMIT.toLocaleString()} combinations. Narrow the ranges.`);
    function visit(i,changes){
      if(i<keys.length){for(const value of values[i])visit(i+1,{...changes,[keys[i]]:value});return;}
      const c={...base,strategy,...changes,entry:{...base.entry},exit:{...base.exit}};
      if('entryValue' in changes)c.entry.value=changes.entryValue;
      if('exitValue' in changes)c.exit.value=changes.exitValue;
      delete c.entryValue;delete c.exitValue;
      // These are expected incompatibilities in otherwise valid Cartesian grids.
      if(c.fast>=c.slow||c.oversold>=c.overbought)return;
      validate(c);results.push(c);
    }
    visit(0,{});
  }
  if(!results.length)throw Error('No valid combinations. Fast must be below slow and RSI entry below exit.');
  return {configs:results,skipped:attempted-results.length};
}
export function splitBars(bars,holdout){
  if(![0,20,30].includes(holdout))throw Error('Choose a supported holdout split.');
  if(!holdout)return {train:bars,holdout:[]};
  const cut=Math.floor(bars.length*(1-holdout/100));
  if(cut<30||bars.length-cut<30)throw Error('Holdout requires at least 30 candles in each segment.');
  return {train:bars.slice(0,cut),holdout:bars.slice(cut)};
}
export function compact(r){return {net:r.net,netPct:r.netPct,maxDD:r.maxDD,winRate:r.winRate,profitFactor:r.profitFactor,trades:r.trades.length};}
export function evaluate(config,segments,id){
  return {id,config,train:compact(backtest(segments.train,config)),holdout:segments.holdout.length?compact(backtest(segments.holdout,config)):null};
}
export function rank(rows,metric,minTrades){
  if(!['netPct','maxDD','profitFactor','winRate'].includes(metric))throw Error('Unknown ranking metric.');
  if(!Number.isInteger(minTrades)||minTrades<1)throw Error('Minimum trades must be a positive whole number.');
  return rows.filter(r=>r.train.trades>=minTrades).sort((a,b)=>{
    const av=a.train[metric],bv=b.train[metric];
    if(av!==bv)return metric==='maxDD'?(av<bv?-1:1):(av>bv?-1:1);
    return b.train.netPct-a.train.netPct||a.id-b.id;
  });
}
