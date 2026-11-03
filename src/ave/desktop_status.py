"""Trusted selected-operation reads. No publisher/create or token-exchange methods."""
from .platforms.connection_verify import identifier
from .publish.adapters.base import Absent

class StatusReader:
    def __init__(self,account,item,*,reader,keychain):self.account=account;self.item=item;self.reader=reader;self.keychain=keychain
    def recover(self,item,record):
        from .publish.receipts import receipt
        a=self.account;p=a.platform
        token=self.keychain.get(a.secret_name('token'))
        if not token:raise ValueError('Connect this account’s credentials first')
        if p in ('instagram','threads'):
            cid=identifier(record.get('container'));version='v21.0' if p=='instagram' else 'v1.0'
            host='https://graph.instagram.com' if p=='instagram' and a.connection.get('product')=='instagram_login' else 'https://graph.facebook.com' if p=='instagram' else 'https://graph.threads.net'
            field='status_code' if p=='instagram' else 'status'
            d=self.reader.request('GET',host+'/'+version+'/'+cid,token=token,params={'fields':field});st=d.get(field)
            if st in ('ERROR','EXPIRED'):return Absent('Provider reported the existing container terminal: '+st)
            if st=='PUBLISHED':return {'receipt':receipt(provider=p,account=a.id,phase='uncertain',object_id=cid,object_kind='container',source='provider_status',diagnostics={'kind':'publication_observed_without_post_identity'})}
            return None
        if p=='tiktok':
            pid=identifier(record.get('publish_id'));d=self.reader.request('POST','https://open.tiktokapis.com/v2/post/publish/status/fetch/',token=token,json_body={'publish_id':pid}).get('data',{});st=d.get('status')
            ids=d.get('publicaly_available_post_id') or []
            if st=='SEND_TO_USER_INBOX':phase='awaiting_user_publish'
            elif st=='PUBLISH_COMPLETE' and ids:phase='published';pid=identifier(ids[0])
            elif st=='FAILED':phase='failed'
            else:return None
            return {'receipt':receipt(provider=p,account=a.id,phase=phase,object_id=pid,object_kind='publish_operation',source='provider_status')}
        raise ValueError('This provider has no supported bounded status read for this operation. Inspect its native destination and use outcome review; do not retry an uncertain post.')
