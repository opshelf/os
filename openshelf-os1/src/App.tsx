import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Switch, Route } from "wouter";
import { Toaster } from "@/components/ui/sonner";
import Header from "@/components/Header";
import RobotController from "@/components/RobotController";
import StorageBuilder from "@/components/StorageBuilder";
import ScaleEventSSE from "@/components/ScaleEvent";
import SizeDatabase from "./components/SizeDatabase";
import ScannerEventSSE from "./components/ScannerEvent";
import Footer from "@/components/Footer";
import Inventory from "@/components/Inventory";
import RobotStatusEventSSE from "@/components/RobotStatusEvent";

const queryClient = new QueryClient();

function App() {
  return (
    <>
      <div className="flex flex-col min-h-screen px-4 mb-18">
        <QueryClientProvider client={queryClient}>
          <Header />
          <Switch>
            <Route path="/inventory" component={Inventory} />
            <Route path="/storage" component={StorageBuilder} />
            <Route path="/sizes" component={SizeDatabase} />
            <Route component={RobotController} />
          </Switch>
        </QueryClientProvider>
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
