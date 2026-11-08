import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import tsconfigPaths from 'vite-tsconfig-paths';

export default defineConfig({
  plugins: [tsconfigPaths()],
  resolve: {
    alias: [
      // Reason: @actual-app/api is linked to a local Actual checkout whose exports have a
      // "development" condition pointing at raw TypeScript sources; use the built bundle instead.
      {
        find: /^@actual-app\/api$/,
        replacement: fileURLToPath(new URL('./node_modules/@actual-app/api/dist/index.js', import.meta.url)),
      },
    ],
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    globals: true,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'json-summary', 'html'],
      include: ['src/core/**/*.ts', 'src/tools/**/*.ts'],
      exclude: ['src/core/**/*.test.ts', 'src/tools/**/*.test.ts', 'src/core/types/domain.ts'],
    },
    alias: {
      '^(\\.{1,2}/.*)\\.js$': '$1', // Handle .js imports in TypeScript
    },
    testTransformMode: {
      web: ['\\.tsx?$'],
    },
  },
});
