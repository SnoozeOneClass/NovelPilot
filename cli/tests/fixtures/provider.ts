import { ModelRuntime } from '@earendil-works/pi-coding-agent';
import {
  InMemoryCredentialStore, InMemoryModelsStore, createAssistantMessageEventStream,
  type AssistantMessage, type Context,
} from '@earendil-works/pi-ai';

export async function scriptedProvider(respond: (index: number, context: Context) => AssistantMessage['content'] | Promise<AssistantMessage['content']>) {
  const requests: Context[] = [];
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(), modelsStore: new InMemoryModelsStore(),
    modelsPath: null, allowModelNetwork: false, refreshOnCreate: false,
  });
  runtime.registerProvider('novelpilot-test', {
    api: 'openai-completions', apiKey: 'test-only', baseUrl: 'https://invalid.example.test',
    models: ['one', 'two'].map((id) => ({
      id, name: id, reasoning: false, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2048,
    })),
    streamSimple: (model, context) => {
      const index = requests.push(context) - 1;
      if (index >= 20) throw new Error('Unbounded test provider calls');
      const stream = createAssistantMessageEventStream();
      const partial: AssistantMessage = {
        role: 'assistant', api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), content: [], stopReason: 'stop',
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      };
      stream.push({ type: 'start', partial });
      void Promise.resolve().then(() => respond(index, context)).then((content) => {
        const reason = content.some((item) => item.type === 'toolCall') ? 'toolUse' : 'stop';
        const message: AssistantMessage = { ...partial, content, stopReason: reason };
        stream.push({ type: 'done', reason, message });
        stream.end(message);
      }, (error: unknown) => {
        const message: AssistantMessage = { ...partial, stopReason: 'error', errorMessage: String(error) };
        stream.push({ type: 'error', reason: 'error', error: message });
        stream.end(message);
      });
      return stream;
    },
  });
  const model = runtime.getModel('novelpilot-test', 'one');
  if (!model) throw new Error('Test model missing');
  return { runtime, model, requests };
}
