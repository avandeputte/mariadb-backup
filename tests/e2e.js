// Browser test of the web UI against a running instance and a real MariaDB.
//   MBM_URL=http://localhost:8090 MBM_PW=testpass123 DB_USER=admin DB_PW=adminpw node tests/e2e.js
// Leaves screenshots in $SHOTS (default ./shots).
const { chromium } = require('playwright');
const URL = process.env.MBM_URL || 'http://localhost:8090';
const SHOTS = process.env.SHOTS || 'shots';
const BACKUP_DIR = process.env.BACKUP_DIR || '/tmp/mbm-backups';

(async () => {
  const fs = require('fs'); fs.mkdirSync(SHOTS, {recursive: true});
  const b = await chromium.launch();
  const p = await b.newPage({viewport: {width: 1280, height: 860}});
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  const shot = n => p.screenshot({path: `${SHOTS}/${n}.png`, fullPage: true});
  const go = h => p.goto(URL + '/' + h);

  await go('');
  await p.fill('#lu', 'admin'); await p.fill('#lp', process.env.MBM_PW || 'testpass123'); await p.click('#loginbtn');
  await p.waitForSelector('text=Getting started'); await shot('01-overview-empty');

  await go('#servers'); await p.click('text=Add server');
  let ins = await p.$$('#dlgform input');
  await ins[0].fill('db1'); await ins[1].fill('127.0.0.1');
  await ins[4].fill(process.env.DB_USER || 'admin'); await ins[5].fill(process.env.DB_PW || 'adminpw');
  await p.click('#dlgform button:has-text("Test")'); await p.waitForSelector('text=Connected: MariaDB');
  await shot('02-server-test');
  await p.click('#dlgform button:has-text("Save")'); await p.waitForSelector('td:has-text("db1")');

  await go('#destinations'); await p.click('text=Add destination');
  ins = await p.$$('#dlgform input'); await ins[0].fill('Local disk'); await ins[1].fill(BACKUP_DIR);
  await p.click('#dlgform button:has-text("Save")'); await p.waitForSelector('td:has-text("Local disk")');

  // the Google Drive form renders (signing in needs a real Google account)
  await p.click('text=Add destination');
  await p.selectOption('#dlgform select', 'gdrive');
  await p.waitForSelector('#dlgform >> text=OAuth client ID', {timeout: 5000}).catch(async e => { await shot('debug-gdrive'); throw e; });
  await shot('02b-gdrive-form');
  await p.click('#dlgform button:has-text("Cancel")');

  await go('#jobs'); await p.click('text=New job');
  await p.fill('#dlgform input[placeholder=nightly]', 'nightly');
  await p.fill('#dlgform input[type=number][min="0"]', '2');
  await shot('03-job-editor');
  await p.click('#dlgform button:has-text("Create job")'); await p.waitForSelector('td:has-text("nightly")');
  await shot('04-jobs');

  for (let i = 0; i < 3; i++) {   // keep=2, so the third run prunes one
    await go('#jobs'); await p.waitForSelector('button:has-text("Run now"):not([disabled])');
    await p.click('button:has-text("Run now")');
    await p.waitForURL(/#run\//);
    await p.waitForSelector('.log >> text=Finished in', {timeout: 60000});
    await p.waitForTimeout(1100);
  }
  await shot('05-run-log');
  const log = await p.$eval('.log', e => e.textContent);
  if (!/Retention: removed/.test(log)) throw new Error('retention did not prune');

  await go('#backups'); await p.waitForSelector('text=Restore...'); await shot('06-backups');
  const names = await p.$$eval('td .mono', e => e.map(x => x.textContent));
  if (names.length !== 2) throw new Error('expected 2 backups after retention, got ' + names.length);

  // restore: only shop.orders as shop_copy, plus user app@%
  await p.click('text=Restore...'); await p.waitForSelector('text=Users, roles and grants');
  const db = p.locator('.tree .db', {hasText: 'shop'});
  await db.locator('input[type=checkbox]').first().check();
  await db.locator('select').selectOption('some');
  for (const lbl of await db.locator('.objs label').all()) {
    const t = await lbl.textContent();
    const cb = lbl.locator('input');
    if (t.startsWith('orders') || t.startsWith('customers')) await cb.check(); else await cb.uncheck();
  }
  await db.locator('input.target').fill('shop_copy');
  await p.locator('.chips label', {hasText: 'app@%'}).locator('input').check();
  await shot('07-restore-pick');
  await p.click('text=Review restore...');
  await p.waitForSelector('#dlgform button:has-text("Restore now")');
  await shot('08-restore-review');
  await p.click('#dlgform button:has-text("Restore now")');
  await p.waitForURL(/#run\//);
  await p.waitForSelector('.log >> text=Finished in', {timeout: 60000});
  await shot('09-restore-done');

  await go('#overview'); await p.waitForSelector('text=Recent activity'); await shot('10-overview');
  await p.setViewportSize({width: 390, height: 800}); await go('#jobs'); await p.waitForTimeout(500); await shot('11-mobile-jobs');
  await p.emulateMedia({colorScheme: 'dark'});
  await p.evaluate(() => localStorage.setItem('mbm_theme', 'dark'));
  await p.setViewportSize({width: 1280, height: 860}); await go('#backups'); await p.reload(); await p.waitForSelector('text=Restore...'); await shot('12-dark-backups');

  if (errs.length) { console.log('PAGE ERRORS', errs); process.exitCode = 1; }
  else console.log('e2e ok');
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
