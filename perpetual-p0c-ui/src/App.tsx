import Storage from './components/Storage';
import {
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query';
import { Switch, Route } from 'wouter';
import Header from './components/Header';

const queryClient = new QueryClient();

function App() {
  return (
    <div className="flex flex-col min-h-screen px-4">
      <QueryClientProvider client={queryClient}>
        <Header />
        <Switch>
          <Route path="/storage-designer" component={Storage} />
          <Route component={Storage} />
        </Switch>
      </QueryClientProvider>
    </div>
  );
}

export default App;
