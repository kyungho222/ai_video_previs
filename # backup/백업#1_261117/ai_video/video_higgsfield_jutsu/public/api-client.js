const LOCAL_HOSTS = ['localhost', '127.0.0.1', '::1'];
const PRODUCTION_API = 'https://api-aipro.chatbaram.com/v1/previs';

export function createApiClient({
  hostname = globalThis.location?.hostname ?? 'localhost',
  errorOrder = ['error', 'detail'],
  defaultMessage = '요청 실패',
  attachStatus = false,
  fetchImpl = globalThis.fetch.bind(globalThis),
} = {}) {
  const baseUrl = LOCAL_HOSTS.includes(hostname) ? '' : PRODUCTION_API;
  let videoPrefix = '/api/video/';

  function apiUrl(raw) {
    const value = typeof raw === 'string' ?
      raw.replace('/api/video/', videoPrefix) :
      raw;
    return typeof value === 'string' && value.startsWith('/api/') ?
      baseUrl + value :
      value;
  }

  async function request(url, options) {
    let response = await fetchImpl(apiUrl(url), options);
    const configRead = url.startsWith('/api/video/config') &&
      (!options?.method || options.method === 'GET');
    if (response.status === 404 && configRead) {
      videoPrefix = '/api/wan/';
      response = await fetchImpl(apiUrl(url), options);
    }
    const payload = await response.json();
    if (!response.ok) {
      const message = errorOrder.map(key => payload[key]).find(Boolean) ?? defaultMessage;
      const error = Error(message);
      if (attachStatus) error.status = response.status;
      throw error;
    }
    return payload;
  }

  return {
    apiUrl,
    request
  };
}
