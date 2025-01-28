import Storage from './components/Storage';
import {
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query';
import { Switch, Route } from 'wouter';
import Header from './components/Header';
import RobotController from './components/RobotControl';
import SubsystemController from './components/SubsystemControl';
import Inventory from './components/Inventory';
import AxisControl from './components/AxisControl';
import SizeDatabase from './components/SizeDatabase';
import ScannerEventSSE from './components/ScannerEvent';
import RobotConfiguration from './components/RobotConfiguration';
import Footer from './components/Footer';

const queryClient = new QueryClient();

function App() {
  return (
    <>
      <div className="flex flex-col min-h-screen px-4">
        <QueryClientProvider client={queryClient}>
          <Header />
          <Switch>
            <Route path="/storage-designer" component={Storage} />
            <Route
              path="/subsystem"
              component={SubsystemController}
            />
            <Route path="/robot" component={RobotController} />
            <Route path="/inventory" component={Inventory} />
            <Route path="/low-level" component={AxisControl} />
            <Route path="/sizes" component={SizeDatabase} />
            <Route path="/settings" component={RobotConfiguration} />
          </Switch>
        </QueryClientProvider>
        <ScannerEventSSE />
      </div>
      <Footer />
    </>
  );
}

export default App;
