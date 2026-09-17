import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

/**
 * TS-019 开发模式专用：与 `vite.config.ts` 只差一件事 —— 把同源 `/api/web/*` 转给合成装置里的
 * 第二个后台实例。
 *
 * 生产入口由后台自己托管静态文件，`/api/web/*` 与页面本来同源。`npm run dev` 的 5173 只有页面，
 * 没有后台，所以 StrictMode 用例需要一个转发，否则请求会跨到 4815 并被真实的 Origin/主机复核
 * 拒绝 —— 那是复核在正常工作，不是页面缺陷。转发保留浏览器自己的 Host，而该后台实例被配置的
 * origin 就是 5173，两项复核因此都比对同一个地址，规则没有被放宽。
 *
 * 这里有意不 import 正式的 `vite.config.ts`：那份配置属于共享入口，而开发模式套件只需要同一套
 * root/插件/端口，多写三行比让正式配置多出一个导出更干净。
 */
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api/web": {
        target: "http://127.0.0.1:4815",
        changeOrigin: false,
      },
    },
  },
  preview: { port: 4173, strictPort: true },
  build: { outDir: "dist", emptyOutDir: true },
});
