"""Stress test: python load.py http://SERVER:30080 SECONDS PARALLEL_CLIENTS"""
import sys, threading, time, urllib.request

url, secs, conc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
img = open("scan.jpg", "rb").read()
B = "----renewguardboundary"
body = (f"--{B}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"scan.jpg\"\r\n"
        f"Content-Type: image/jpeg\r\n\r\n").encode() + img + f"\r\n--{B}--\r\n".encode()
end, ok, err = time.time() + secs, [0], [0]


def worker():
    while time.time() < end:
        req = urllib.request.Request(url + "/api/enhance", body,
                                     {"Content-Type": f"multipart/form-data; boundary={B}"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
            ok[0] += 1
        except Exception:
            err[0] += 1


ts = [threading.Thread(target=worker) for _ in range(conc)]
[t.start() for t in ts]
while time.time() < end:
    time.sleep(5)
    print(f"ok={ok[0]} err={err[0]}", flush=True)
[t.join() for t in ts]
