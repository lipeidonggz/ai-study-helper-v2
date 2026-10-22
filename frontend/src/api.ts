/** 本地 API 请求类别对应的前端等待上限。 */
export const API_TIMEOUT_MS = {
  /** 健康检查、设置读写、任务查询和控制等短请求。 */
  quick: 15_000,
  /** 真实模型 Key 连通性测试，需覆盖后端 60 秒供应商超时和响应解析。 */
  providerTest: 75_000,
  /** URL 来源预览，需覆盖 URL 获取、解析和预览 artifact 写入。 */
  sourcePreview: 90_000,
  /** 清空知识库等多文件维护操作。 */
  maintenance: 60_000,
} as const;

/** 为本地 API 请求添加硬超时，同时保留调用方取消信号并区分超时与主动取消。 */
export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs: number = API_TIMEOUT_MS.quick,
): Promise<Response> {
  const controller = new AbortController();
  let didTimeout = false;
  const abortFromCaller = (): void => controller.abort(init.signal?.reason);

  if (init.signal?.aborted) {
    abortFromCaller();
  } else {
    init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeoutId = window.setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await fetch(input, {
      ...init,
      signal: controller.signal,
    });
  } catch (error) {
    if (didTimeout) {
      throw new Error(
        `请求超时：超过 ${Math.ceil(timeoutMs / 1000)} 秒未完成响应，请稍后重试。`,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}
