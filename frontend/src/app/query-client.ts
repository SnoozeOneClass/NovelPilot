import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 4,
      retryDelay: (attemptIndex) => Math.min(750 * 2 ** attemptIndex, 3_000),
      staleTime: 3_000
    },
    mutations: {
      retry: false
    }
  }
});
