import json, os, threading, tempfile, wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

server=ThreadingHTTPServer(('127.0.0.1',0),partial(SimpleHTTPRequestHandler,directory=str(Path(__file__).resolve().parents[2])))
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as p:
        src=Path(temp)/'side.wav'
        peaks=Path(temp)/'side.json'
        with wave.open(str(src),'wb') as f:
            f.setparams((2,2,48000,0,'NONE','not compressed'))
            f.writeframes(b'\0'*48000*4*2)
        peaks.write_text(json.dumps({'frames':96000,'sample_rate':48000,'peaks':[[0,0],[0,0]]}))
        browser=p.chromium.launch(executable_path=os.environ.get('CHROME_PATH'))
        for width in (390, 1280):
            page=browser.new_page(viewport={'width':width,'height':850})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/review/')
            page.locator('#recording').set_input_files(src)
            page.locator('#waveform').set_input_files(peaks)
            page.wait_for_function('!document.querySelector("#add").disabled')
            page.get_by_role('button',name='Save track start').click()
            page.locator('#seconds').fill('1')
            page.get_by_role('button',name='Save track start').click()
            page.get_by_role('button',name='Edit track start at 1.000 seconds').click()
            page.locator('#seconds').fill('1.5')
            page.get_by_role('button',name='Save track start').click()
            with page.expect_download() as download:
                page.get_by_role('button',name='Download split plan').click()
            plan=json.loads(download.value.path().read_text())
            assert plan['tracks']==[{'start':0,'end':72000},{'start':72000,'end':96000}]
            page.get_by_role('button',name='Remove track start at 0.000 seconds').click()
            assert page.locator('#splits li').count()==1
            assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
            assert not errors, errors
            page.close()
            print(f'PASS: load, edit, export, remove, responsive bounds at {width}px')
        browser.close()
finally:
    server.shutdown()
    server.server_close()
