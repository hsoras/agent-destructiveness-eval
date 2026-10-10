import { Effect, Stream, Deferred } from 'effect';
import { spawn } from 'node:child_process';
import { writeFileSync, readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { makeRun as original } from './original.ts';
import { makeRun as patched } from './patched.ts';
const results=[];
async function probe(name, makeRun, fixture) {
 const events=[]; let child; let outputFile; const permit=await Effect.runPromise(Deferred.make());
 const abort=new AbortController();
 const spawner={spawn:()=>Effect.gen(function*(){
  child=spawn(process.execPath,['-e',fixture.program??"process.stdout.write('-x\\n')"],{stdio:['ignore','pipe','pipe']});
  yield* Effect.addFinalizer(()=>Effect.sync(()=>{events.push('scope-finalized');child.kill('SIGKILL'); child.stdout.destroy();child.stderr.destroy();}));
  const exitPromise=new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',(code)=>{events.push('process-exit');resolve(code);if(fixture.abortDrain)setImmediate(()=>abort.abort());if(fixture.gated)setImmediate(()=>{events.push('read-permit');Effect.runSync(Deferred.succeed(permit,undefined));});});});
  // Subscribe immediately, like a process adapter: queue real pipe bytes independently
  // of the controlled Effect consumer. Node otherwise auto-resumes unread stdio at exit.
  const buffered=new ReadableStream({start(controller){
   let ended=0;
   for(const pipe of [child.stdout,child.stderr]){
    pipe.on('data',chunk=>{events.push('pipe-bytes');controller.enqueue(chunk);});
    pipe.on('end',()=>{if(++ended===2)controller.close();});
    pipe.on('error',error=>controller.error(error));
   }
  }});
  let all=Stream.fromAsyncIterable(buffered,e=>e);
  if(fixture.never)all=Stream.concat(all,Stream.never);
  if(fixture.error)all=Stream.fail(new Error('controlled collector failure'));
  if(fixture.gated)all=Stream.unwrap(Deferred.await(permit).pipe(Effect.as(all)));
  return {all,exitCode:Effect.promise(()=>exitPromise),kill:()=>Effect.sync(()=>{events.push('kill');child.kill('SIGTERM');})};
 })};
 const trunc={limits:()=>Effect.succeed({maxBytes:fixture.truncate?64:10000,maxLines:100}),write:(s)=>Effect.sync(()=>{outputFile=`${process.cwd()}/${name}-full.txt`;writeFileSync(outputFile,s);return outputFile;})};
 const ctx={abort:abort.signal,metadata:()=>Effect.void};
 const run=makeRun(spawner,trunc,()=>undefined);
 if(fixture.abort)setTimeout(()=>abort.abort(),50);
 const started=performance.now();
 const exit=await Effect.runPromiseExit(run({shell:'/bin/sh',command:'benign controlled Node child',cwd:process.cwd(),env:process.env,timeout:fixture.timeout??1500,description:name},ctx));
 await new Promise(resolve=>setImmediate(resolve));
 const item={name,elapsedMs:Math.round(performance.now()-started),events,exit:exit._tag};
 if(exit._tag==='Success'){item.result=exit.value; if(outputFile)item.fullOutput=readFileSync(outputFile,'utf8');}
 else item.failure=String(exit.cause);
 results.push(item);
 if(fixture.expected!==undefined)assert.equal(exit.value?.output,fixture.expected,name);
 if(fixture.expectedCode!==undefined)assert.equal(exit.value?.metadata.exit,fixture.expectedCode,name);
 if(fixture.expectedMeta)assert.ok(exit.value?.output.includes(fixture.expectedMeta),name);
 if(fixture.truncate){assert.ok(exit.value.metadata.truncated);assert.equal(item.fullOutput,'abc\n'.repeat(100));}
 if(fixture.error)assert.equal(exit._tag,'Failure');
 if(fixture.bound)assert.ok(item.elapsedMs<fixture.bound,name);
 console.log(JSON.stringify(item));
}
try {
 await probe('original-pending-pipe',original,{gated:true,expected:'(no output)',expectedCode:0});
 await probe('joined-pending-pipe',patched,{gated:true,expected:'-x\n',expectedCode:0});
 await probe('joined-stderr-nonzero',patched,{gated:true,program:"process.stderr.write('benign error\\n');process.exitCode=7",expected:'benign error\n',expectedCode:7});
 await probe('joined-empty',patched,{gated:true,program:'process.exitCode=0',expected:'(no output)',expectedCode:0});
 await probe('joined-open-stream',patched,{never:true,timeout:100,expectedMeta:'exceeding timeout',expectedCode:null,bound:1200});
 await probe('joined-abort-during-drain',patched,{never:true,abortDrain:true,expectedMeta:'User aborted',expectedCode:null,bound:1200});
 await probe('joined-live-timeout',patched,{program:"process.stdout.write('before timeout\\n');setInterval(()=>{},1000)",timeout:100,expectedMeta:'exceeding timeout',expectedCode:null,bound:1200});
 await probe('joined-live-abort',patched,{program:"process.stdout.write('before abort\\n');setInterval(()=>{},1000)",abort:true,expectedMeta:'User aborted',expectedCode:null,bound:1200});
 await probe('joined-truncation',patched,{gated:true,program:"process.stdout.write('abc\\n'.repeat(100))",truncate:true,expectedCode:0});
 await probe('joined-collector-error',patched,{error:true,bound:1200});
 writeFileSync('results.json',JSON.stringify({status:'all assertions passed',results},null,2));
} catch(e){writeFileSync('results.json',JSON.stringify({status:'failed',error:String(e),results},null,2));throw e;}
