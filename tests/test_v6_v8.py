import sqlite3
from v6_semantic import index_documents,search
from v8_autonomous import detect
def db():
 c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row
 c.execute("CREATE TABLE documents(readwise_id TEXT PRIMARY KEY,title TEXT,content TEXT,canonical_text TEXT)")
 c.execute("CREATE TABLE document_stages(readwise_id TEXT,stage TEXT,status TEXT,updated_at TEXT)")
 return c
def test_v6():
 c=db(); d=[{"readwise_id":"1","title":"AI chips","canonical_text":"GPU semiconductor memory"},{"readwise_id":"2","title":"Food","canonical_text":"pasta tomato olive oil"}]
 assert index_documents(c,d)["added"]==2 and search(c,"AI semiconductor",1)[0]["readwise_id"]=="1"
def test_v8():
 c=db(); d=[{"readwise_id":"1","title":"AI","canonical_text":"AI chips"}]; index_documents(c,d); c.execute("INSERT INTO document_stages VALUES('1','rating','completed','2026-01-01')"); c.commit(); assert detect(c,d)["checked"]==1
