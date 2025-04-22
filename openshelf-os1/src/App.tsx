import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Switch, Route } from "wouter";
import { Toaster } from "@/components/ui/sonner";
import Header from "@/components/Header";
import RobotController from "@/components/RobotController";
import StorageBuilder from "@/components/StorageBuilder";
import ScaleEventSSE from "@/components/ScaleEvent";
// import SubsystemController from './components/SubsystemControl';
// import Inventory from './components/Inventory';
// import AxisControl from './components/AxisControl';
// import SizeDatabase from './components/SizeDatabase';
import ScannerEventSSE from "./components/ScannerEvent";
// import RobotConfiguration from './components/RobotConfiguration';
import Footer from "@/components/Footer";
// import AxisController from './components/AxisController';
import RobotStatusEventSSE from "@/components/RobotStatusEvent";

const queryClient = new QueryClient();

function App() {
  return (
    <>
      <div className="flex flex-col min-h-screen px-4">
        <QueryClientProvider client={queryClient}>
          <Header />
          <Switch>
            <Route path="/storage" component={StorageBuilder} />
            {/*
            <Route
              path="/subsystem"
              component={SubsystemController}
            />
            <Route path="/move" component={AxisController} />
            <Route path="/inventory" component={Inventory} />
            <Route path="/low-level" component={AxisControl} />
            <Route path="/sizes" component={SizeDatabase} />
            <Route path="/settings" component={RobotConfiguration} />
            <Route path="/robot" component={RobotController} />
            */}
            <Route component={RobotController} />
          </Switch>
        </QueryClientProvider>
        {/*
        <ScannerEventSSE />
        <StatusEventSSE />
        */}
        <ScaleEventSSE />
        <ScannerEventSSE />
        <RobotStatusEventSSE />
        <Footer />
        <Toaster />
      </div>
    </>
  );
}

export default App;
