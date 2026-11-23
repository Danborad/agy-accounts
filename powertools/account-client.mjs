import { readFileSync } from 'node:fs';

export function createAccountClient({ base = 'http://127.0.0.1:8790',
                                     keyFile = '/etc/agy-powertools/accounts.key', fetcher = fetch } = {}) {
  const url = new URL('/internal/powertools/accounts', base);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)) throw Error('账号后端仅限回环地址');
  return async (action, payload = {}) => {
    const key = readFileSync(keyFile, 'utf8').trim();
    if (!/^[a-f0-9]{64}$/.test(key)) throw Error('账号桥接密钥无效');
    const response = await fetcher(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Agy-Accounts-Bridge': key },
      body: JSON.stringify({ ...payload, action }), signal: AbortSignal.timeout(10000), redirect: 'error',
    });
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw Error(data.error || '账号后端暂时不可用');
    return data;
  };
}
