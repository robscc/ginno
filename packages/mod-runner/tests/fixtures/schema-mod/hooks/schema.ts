// The configSchema fixture: a `defineMod` module that declares both defaults
// and the optional JSON-Schema subset the settings editor renders.

import { defineMod } from '../../../../src/define-mod.ts'

export default defineMod({
  name: 'schema-mod',
  version: '0.1.0',
  userConfig: { units: 'metric', limit: 3 },
  configSchema: {
    type: 'object',
    properties: {
      units: {
        type: 'string',
        enum: ['metric', 'imperial'],
        title: 'Units',
        description: 'Display units',
        default: 'metric',
        'x-ginno-group': 'Display',
        'x-ginno-order': 10,
      },
      apiKey: { type: 'string', default: '', 'x-ginno-secret': true },
      tags: { type: 'array', items: { type: 'string' }, default: [] },
    },
  },
  register() {},
})