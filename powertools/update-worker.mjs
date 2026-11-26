import { readFileSync, writeFileSync, mkdirSync, renameSync, existsSync, readdirSync, lstatSync, mkdtempSync, unlinkSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { homedir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { downloadReleaseAsset, decodeVerifiedPackage, writeVerifiedPackage } from './update-package.mjs';

export async function runPluginUpdate({ currentDir, root, version, release, verify, restart, setState,
                                        fetcher = fetch, node = process.execPath }) {
  // Keep rename operations within the same systemd bind mount/filesystem.
  const backups = join(dirname(currentDir), '.agy-powertools-web-updates');
  mkdirSync(backups, { recursive: true, mode: 0o700 });
  const job = mkdtempSync(join(backups, 'job-'));
  const verification = mkdtempSync(join(root, 'verification-'));
  const candidate = join(job, 'candidate');
  const backup = join(job, 'previous');
  let promoted = false;
  let moved = false;
  function assertPlainTree(path) {
    const stat = lstatSync(path);
    if (stat.isSymbolicLink()) throw Error('插件目录含符号链接，取消更新');
    if (stat.isDirectory()) for (const name of readdirSync(path)) assertPlainTree(join(path, name));
  }
  function pruneOldBackups(dir, keep = 3) {
    try {
      const jobs = readdirSync(dir)
        .filter(name => name.startsWith('job-'))
        .map(name => ({ path: join(dir, name), mtime: lstatSync(join(dir, name)).mtimeMs }))
        .sort((a, b) => b.mtime - a.mtime);
      for (const stale of jobs.slice(keep)) rmSync(stale.path, { recursive: true, force: true });
    } catch { /* Backup cleanup is best-effort. */ }
  }
  try {
    if (release.status !== 'available' || release.version !== version || !release.canInstall || !release.asset) throw Error('版本信息已变化，请重新检查更新');
    setState({ status: 'downloading', version });
    const asset = release.asset;
    let expected = asset.sha256;
    if (!expected && asset.checksumUrl) {
      const checksum = (await downloadReleaseAsset(asset.checksumUrl, 4096, fetcher)).toString('utf8');
      const match = checksum.trim().match(/^([a-f0-9]{64})\s+\*?([^\r\n]+)$/i);
      if (match?.[2] !== asset.name) throw Error('校验文件与安装包不一致');
      expected = match[1].toLowerCase();
    }
    const bytes = await downloadReleaseAsset(asset.url, 16 * 1024 * 1024, fetcher);
    setState({ status: 'verifying', version });
    const bundle = decodeVerifiedPackage(bytes, expected, version);
    writeVerifiedPackage(bundle, candidate);
    // Preserve host-specific Node path, but do not reuse old files from the previous release.
    const sidecarPath = join(candidate, 'sidecars', 'inspector', 'sidecar.json');
    const sidecar = JSON.parse(readFileSync(sidecarPath));
    sidecar.command = node;
    writeFileSync(sidecarPath, JSON.stringify(sidecar, null, 2));
    writeFileSync(join(candidate, '.agy-install.json'), JSON.stringify({
      id: bundle.id, version, files: bundle.files.map(({ path, sha256 }) => ({ path, sha256 })),
      webManaged: true, localOverrides: ['sidecars/inspector/sidecar.json'],
    }), { mode: 0o600 });
    await verify(candidate, join(verification, 'candidate'));
    assertPlainTree(currentDir);
    const old = JSON.parse(readFileSync(join(currentDir, 'plugin.json')));
    if (old.name !== 'agy-powertools') throw Error('目标目录不是 Powertools');
    setState({ status: 'installing', version, backup });
    // Plugin data/credentials live elsewhere and are never copied or replaced.
    renameSync(currentDir, backup);
    moved = true;
    renameSync(candidate, currentDir);
    promoted = true;
    await restart();
    await verify(currentDir, join(verification, 'installed'));
    setState({ status: 'completed', version, backup, message: '插件已更新，请刷新网页加载新版' });
    pruneOldBackups(backups);
    return { version, backup };
  } catch (error) {
    if (moved) {
      try {
        if (promoted) renameSync(currentDir, join(job, 'failed-candidate'));
        renameSync(backup, currentDir);
        await restart();
        await verify(currentDir, join(verification, 'rollback'));
        setState({ status: 'rolled-back', version, message: `更新未通过验证，已恢复旧版：${error.message}` });
      } catch {
        setState({ status: 'failed', version, backup: existsSync(backup) ? backup : currentDir,
          message: '回滚未通过验证，旧版文件已保留，请管理员检查插件桥接' });
      }
    } else {
      setState({ status: 'failed', version, message: `未修改现有插件：${error.message}` });
    }
    throw error;
  } finally {
    rmSync(verification, { recursive: true, force: true });
  }
}

if (process.argv[1]?.endsWith('/update-worker.mjs')) {
  const home = homedir();
  const root = join(home, '.local', 'share', 'agy-accounts', 'powertools-updates');
  const planPath = join(root, 'plan.json');
  const statePath = join(root, 'state.json');
  const setState = value => {
    const tmp = statePath + '.tmp';
    writeFileSync(tmp, JSON.stringify({ ...value, at: Date.now() }), { mode: 0o600 });
    renameSync(tmp, statePath);
  };
  let version;
  let maintenanceLock;
  try {
    const plan = JSON.parse(readFileSync(planPath));
    version = plan.version;
    if (!/^\d+\.\d+\.\d+$/.test(version || '') || Date.now() - plan.at > 60000 || plan.at > Date.now()) throw Error('更新计划无效或已过期');
    const currentDir = join(home, '.gemini', 'config', 'plugins', 'agy-powertools');
    maintenanceLock = join(home, '.gemini', 'antigravity', 'plugin_data', 'agy-powertools', 'maintenance.lock');
    if (existsSync(maintenanceLock)) {
      const record = JSON.parse(readFileSync(maintenanceLock));
      try { process.kill(record.pid, 0); throw Error('桌面插件安装或更新正在进行'); }
      catch (error) { if (error.code !== 'ESRCH') throw error; }
      unlinkSync(maintenanceLock);
    }
    writeFileSync(maintenanceLock, JSON.stringify({ pid: process.pid, nonce: 'web-update' }), { flag: 'wx', mode: 0o600 });
    const currentVersion = JSON.parse(readFileSync(join(currentDir, 'plugin.json'))).version;
    const { checkForUpdates } = await import(pathToFileURL(join(currentDir, 'lib', 'updates.mjs')));
    const release = await checkForUpdates({ currentVersion, project: { githubUrl: 'https://github.com/WhiteBr1ck/agy-powertools' } });
    const verify = async (directory, data) => {
      mkdirSync(data, { recursive: true, mode: 0o700 });
      const result = spawnSync(process.execPath, [new URL('./verify-plugin.mjs', import.meta.url).pathname, '--verify', directory, data],
        { timeout: 65000, encoding: 'utf8', maxBuffer: 256 * 1024 });
      if (result.status !== 0) throw Error('插件入口或网页适配未通过兼容验证');
    };
    const restart = async () => {
      const expected = JSON.parse(readFileSync(join(currentDir, 'plugin.json'))).version;
      const result = spawnSync('systemctl', ['restart', 'agy-powertools-web'], { timeout: 20000 });
      if (result.status !== 0) throw Error('重启插件桥接失败');
      const deadline = Date.now() + 10000;
      while (Date.now() < deadline) {
        try {
          const health = await fetch('http://127.0.0.1:8791/healthz', { signal: AbortSignal.timeout(1000) });
          if (health.ok && (await health.json()).version === expected) return;
        } catch { /* The service is still starting. */ }
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      throw Error('插件桥接启动未通过健康检查');
    };
    await runPluginUpdate({ currentDir, root, version, release, verify, restart, setState });
  } catch (error) {
    const status = existsSync(statePath) ? JSON.parse(readFileSync(statePath)).status : '';
    if (!['failed', 'rolled-back'].includes(status)) setState({ status: 'failed', version, message: error.message });
    console.error('Powertools 更新未完成：', error.message);
    process.exitCode = 1;
  } finally {
    if (maintenanceLock && existsSync(maintenanceLock)) {
      try {
        const owner = JSON.parse(readFileSync(maintenanceLock));
        if (owner.pid === process.pid) unlinkSync(maintenanceLock);
      } catch { /* Never mask the original update error. */ }
    }
  }
}
