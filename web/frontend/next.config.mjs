/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // 后端 FastAPI 默认运行在 127.0.0.1:8000；前端通过 NEXT_PUBLIC_API_BASE_URL 覆盖。
  async rewrites() {
    return []
  },
};

export default nextConfig;
