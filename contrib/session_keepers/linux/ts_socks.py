import os,socket,sys,struct,threading
h,p=sys.argv[1],int(sys.argv[2])
s=socket.create_connection(("127.0.0.1",1055))
s.sendall(b"\x05\x01\x00");s.recv(2)
s.sendall(b"\x05\x01\x00\x03"+bytes([len(h)])+h.encode()+struct.pack(">H",p))
r=s.recv(10)
def a():
  while True:
    try:
      d=os.read(0,65536)
    except Exception:
      break
    if not d:break
    s.sendall(d)
threading.Thread(target=a,daemon=True).start()
while True:
  d=s.recv(65536)
  if not d:break
  os.write(1,d)
os._exit(0)
