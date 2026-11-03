import assert from 'node:assert/strict';
import test from 'node:test';
import { fridayGatewayClient, FRIDAY_US_BASE_URL } from '../src/friday-gateway.js';

test('Friday client binds the US endpoint and user credential', () => {
  const client = fridayGatewayClient('user-key', 'api_key');
  assert.equal(client.baseURL, FRIDAY_US_BASE_URL);
  assert.equal(client.apiKey, 'user-key');
  assert.throws(() => fridayGatewayClient('bad token'));
});
