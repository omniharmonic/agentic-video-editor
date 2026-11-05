// Shared projection of persisted evidence. No mutation or retry action belongs here.
import {h} from './panels.js';
const PHASES={published:'Primary post published',processing:'Processing at provider',scheduled:'Scheduled at provider',awaiting_user_publish:'Awaiting your publication in the platform app',sandbox:'Sandbox only — not a public post',draft:'Provider draft — not published',failed:'Provider operation failed',uncertain:'Publication outcome is uncertain'};
export function receiptReason(v){const r=v?.result?.receipt;if(!r)return null;if(r.source==='human_report'&&r.diagnostics?.decision==='not_published')return 'Not published · reported by a person. Fresh human review and approval are required before another send.';return v.status_reason||(r.source==='human_report'&&r.phase==='published'?'Published · reported by a person':PHASES[r.phase]);}
export function deliveryReceipt(v){
  const r=v?.result?.receipt;if(!r)return null;
  const provenance=r.source==='human_report'?(r.diagnostics?.decision==='not_published'?'A person reported that this operation did not publish. This is not provider verification.':'Publication reported by a person.'):'Evidence from the provider response.';
  const box=h('section.delivery-receipt',{'aria-label':'Delivery outcome'},h('p',h('strong',receiptReason(v))),h('p.st-hint',provenance));
  const fc=r.adjuncts?.first_comment;
  if(fc){const state=fc.state==='sending'?'uncertain':fc.state;box.append(h('p.delivery-adjunct',`First comment: ${state}.`));if(['uncertain','failed'].includes(state))box.append(h('p',state==='uncertain'?'The comment may already exist. Inspect the platform before adding it manually. The primary post will not be sent again.':'The primary post is published. Inspect the platform to add the comment manually; the primary post will not be sent again.'));}
  let url=r.publication?.url||v.result.url;
  if(!url&&fc)url={instagram:'https://www.instagram.com/',threads:'https://www.threads.net/'}[v.platform];
  try{const parsed=new URL(url);if(['https:','http:'].includes(parsed.protocol))box.append(h('a.btn',{href:parsed.href,target:'_blank',rel:'noopener noreferrer'},'Inspect on platform'));}catch{}
  return box;
}
