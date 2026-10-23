import {readdirSync,readFileSync,mkdirSync,writeFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve,relative,sep} from 'node:path';

const root=fileURLToPath(new URL('../',import.meta.url));
const dist=resolve(root,'dist');
const origin='https://cosens.ai/';
const resources=new Map();
for(const file of readdirSync(dist,{recursive:true})){
  if(file==='index.html'||file.endsWith(`${sep}index.html`)){
    const path=file.split(sep).join('/');
    const url=new URL(path.slice(0,-'index.html'.length),origin);
    resources.set(url.href,resolve(dist,file));
    const html=readFileSync(resolve(dist,file),'utf8');
    for(const attribute of ['href="','src="']){
      for(const fragment of html.split(attribute).slice(1)){
        const value=fragment.split('"')[0];
        if(!value.includes('?v='))continue;
        const asset=new URL(value,url);
        if(asset.origin!==url.origin)continue;
        const local=resolve(dist,`.${asset.pathname}`);
        if(relative(dist,local).startsWith('..'))throw new Error(`Asset escapes dist: ${asset.pathname}`);
        resources.set(asset.href,local);
      }
    }
  }
}

// Compare actual response bytes, rather than treating HTTP 200 as release verification.
for(const [url,file] of resources){
  const expected=readFileSync(file);
  let matched=false;
  for(let attempt=0;attempt<10;attempt++){
    const actual=execFileSync('curl',['--fail','--silent','--show-error','--location','--max-time','20',url]);
    if(actual.equals(expected)){matched=true;break;}
    if(attempt<9)await new Promise(resolve=>setTimeout(resolve,2000));
  }
  if(!matched)throw new Error(`Production content differs from this checkout: ${url}`);
  console.log(`Verified ${url}`);
}
const commit=process.env.COSENS_DEPLOY_COMMIT??execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim();
const receipt={verifiedAt:new Date().toISOString(),commit,project:'cosens',origin,verifiedUrls:[...resources.keys()]};
const directory=resolve(root,'.wrangler');
mkdirSync(directory,{recursive:true});
writeFileSync(resolve(directory,'deploy-receipt.json'),JSON.stringify(receipt,null,2)+'\n');
console.log(`Production verified: ${resources.size} resources. Receipt: .wrangler/deploy-receipt.json`);
