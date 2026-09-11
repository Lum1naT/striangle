import {buildGrid,splitBars,evaluate,LIMIT,WORK_LIMIT} from './optimizer.mjs';
self.onmessage=({data})=>{
  try{
    const {configs,skipped}=buildGrid(data.base,data.spec);
    if(configs.length*data.bars.length>WORK_LIMIT)throw Error('Search exceeds 50 million candle evaluations. Reduce history or combinations.');
    const segments=splitBars(data.bars,data.spec.holdout),rows=[];
    self.postMessage({type:'start',total:configs.length,skipped});
    let last=performance.now();
    for(let i=0;i<configs.length;i++){
      rows.push(evaluate(configs[i],segments,i));
      if(performance.now()-last>150||i===configs.length-1){
        self.postMessage({type:'batch',rows:rows.splice(0),completed:i+1,total:configs.length});last=performance.now();
      }
    }
    self.postMessage({type:'done',total:configs.length});
  }catch(e){self.postMessage({type:'error',message:e.message});}
};
