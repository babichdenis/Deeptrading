import sys, os, fcntl

chat = "/Users/Denis/Dev/Deeptrading/chat.md"
lock = "/Users/Denis/Dev/Deeptrading/.chat-write.lock"
src = sys.argv[1]

with open(src, encoding="utf-8") as f:
    block = f.read()
if not block.endswith("\n"):
    block += "\n"
if not block.startswith("\n"):
    block = "\n" + block

lk = open(lock, "a")
fcntl.flock(lk, fcntl.LOCK_EX)
try:
    with open(chat, "a", encoding="utf-8") as out:
        out.write(block)
        out.flush()
        os.fsync(out.fileno())
    size = os.path.getsize(chat)
finally:
    fcntl.flock(lk, fcntl.LOCK_UN)
    lk.close()

print("appended_bytes=%d chat_size=%d" % (len(block), size))
