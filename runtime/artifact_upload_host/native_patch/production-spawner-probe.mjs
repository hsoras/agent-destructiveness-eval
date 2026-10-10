import { Effect, Stream, Deferred } from 'effect';
import { CrossSpawnSpawner } from '../core/src/cross-spawn-spawner.ts';
import { ChildProcess } from 'effect/unstable/process';
import { ChildProcessSpawner } from 'effect/unstable/process/ChildProcessSpawner';
import { writeFileSync, readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { makeRun as original } from './original.ts';
import { makeRun as patched } from './patched.ts';
const results=[];
async function probe(name, makeRun, fixture) {
 const events=[]; let child; let outputFile; const permit=await Effect.runPromise(Deferred.make());
 const abort=new AbortController();
 const spawner={spawn:()=>Effect.gen(function*(){
  const native=yield* ChildProcessSpawner;
  const handle=yield* native.spawn(ChildProcess.make(process.execPath,['-e',fixture.program??"process.stdout.write('-x\\n')"],{detached:true,stdin:'ignore'}));
  const buffered=new ReadableStream({start(controller){
   child=controller;
  }});
  yield* Effect.forkScoped(Stream.runForEach(handle.all,chunk=>Effect.sync(()=>{
   events.push('pipe-bytes');child.enqueue(chunk);
  })).pipe(Effect.match({onFailure:error=>child.error(error),onSuccess:()=>child.close()})));
  const exitCode=handle.exitCode.pipe(Effect.tap(()=>Effect.sync(()=>{
   events.push('process-exit');
   if(fixture.abortDrain)setImmediate(()=>abort.abort());
   if(fixture.gated)setImmediate(()=>{events.push('read-permit');Effect.runSync(Deferred.succeed(permit,undefined));});
  })));
  yield* Effect.addFinalizer(()=>Effect.sync(()=>events.push('scope-finalized')));
  let all=Stream.fromAsyncIterable(buffered,e=>e);
  if(fixture.never)all=Stream.concat(all,Stream.never);
  if(fixture.error)all=Stream.fail(new Error('controlled collector failure'));
  if(fixture.gated)all=Stream.unwrap(Deferred.await(permit).pipe(Effect.as(all)));
  return {all,exitCode,kill:handle.kill};
 })};
 const trunc={limits:()=>Effect.succeed({maxBytes:fixture.truncate?64:10000,maxLines:100}),write:(s)=>Effect.sync(()=>{outputFile=`${process.cwd()}/${name}-full.txt`;writeFileSync(outputFile,s);return outputFile;})};
 const ctx={abort:abort.signal,metadata:input=>fixture.metadataError && input.metadata.output ? Effect.fail(new Error('controlled metadata failure')) : Effect.void};
 const run=makeRun(spawner,trunc,()=>undefined);
 if(fixture.abort)setTimeout(()=>abort.abort(),50);
 const started=performance.now();
 const exit=await Effect.runPromiseExit(run({shell:'/bin/sh',command:'benign controlled Node child',cwd:process.cwd(),env:process.env,timeout:fixture.timeout??1500,description:name},ctx).pipe(Effect.provide(CrossSpawnSpawner.defaultLayer)));
 await new Promise(resolve=>setImmediate(resolve));
 const item={name,elapsedMs:Math.round(performance.now()-started),events,exit:exit._tag};
 if(exit._tag==='Success'){item.result=exit.value; if(outputFile)item.fullOutput=readFileSync(outputFile,'utf8');}
 else item.failure=String(exit.cause);
 results.push(item);
 if(fixture.expected!==undefined)assert.equal(exit.value?.output,fixture.expected,name);
 if(fixture.expectedCode!==undefined)assert.equal(exit.value?.metadata.exit,fixture.expectedCode,name);
 if(fixture.expectedMeta)assert.ok(exit.value?.output.includes(fixture.expectedMeta),name);
 if(fixture.truncate){assert.ok(exit.value.metadata.truncated);assert.equal(item.fullOutput,'abc\n'.repeat(100));}
 if(fixture.error || fixture.metadataError)assert.equal(exit._tag,'Failure');
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
 await probe('joined-rapid-exit',patched,{expected:'-x\n',expectedCode:0});
 await probe('joined-multi-chunk-spool',patched,{program:"let i=0;function write(){process.stdout.write('abc\\n');if(++i<100)setImmediate(write)}write()",truncate:true,expectedCode:0});
 await probe('joined-metadata-error',patched,{metadataError:true,bound:1200});
 await probe('joined-descendant-held-pipe',patched,{program:"require('node:child_process').spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:['ignore',1,2]});process.exit(0)",timeout:100,expectedMeta:'exceeding timeout',expectedCode:null,bound:5000});
 await probe('joined-collector-error',patched,{error:true,bound:1200});
 writeFileSync('results.json',JSON.stringify({status:'all assertions passed',results},null,2));
} catch(e){writeFileSync('results.json',JSON.stringify({status:'failed',error:String(e),results},null,2));throw e;}
