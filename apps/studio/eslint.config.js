import js from '@eslint/js'
import vue from 'eslint-plugin-vue'
import vueTsConfig from '@vue/eslint-config-typescript'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'src/api/types.gen.ts', 'src/api/schema.d.ts', '**/*.d.ts'] },
  js.configs.recommended,
  ...vue.configs['flat/recommended'],
  ...vueTsConfig(),
  {
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        window: 'readonly',
        document: 'readonly',
        fetch: 'readonly',
        console: 'readonly',
        RequestInit: 'readonly',
      },
    },
    rules: {
      'vue/multi-word-component-names': 'off',
      // Allow intentionally-unused variables prefixed with `_` (e.g. `catch (_e)`).
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
    },
  },
]
