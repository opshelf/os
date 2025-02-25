#![no_std]
pub mod relay;
use modbus_core::{rtu::RequestAdu, Response};

pub trait Transport {
    type Error;
    #[allow(async_fn_in_trait)]
    async fn execute_transaction<'a>(
        &'a mut self,
        request: RequestAdu<'_>,
        tx_buffer: &mut [u8],
        rx_buffer: &'a mut [u8],
    ) -> Result<Response<'a>, Self::Error>;
}

//impl Transport for SerialStream {
//    async fn execute_transaction<'a>(
//        &'a mut self,
//        request: RequestAdu<'_>,
//        tx_buffer: &mut [u8],
//        rx_buffer: &'a mut [u8],
//    ) -> Result<Response<'a>> {
//        let size = encode_request(request, tx_buffer).map_err(|e| anyhow!(e))?;
//        self.write_all(&tx_buffer[0..size]).await?;
//        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
//        let n = self.read(rx_buffer).await?;
//        match decode_response(&rx_buffer[0..n]) {
//            Ok(Some(response)) => match response.pdu.0 {
//                Ok(pdu) => Ok(pdu),
//                Err(_) => Err(anyhow!("Illegal Function")),
//            },
//            Ok(None) => Err(anyhow!("No Response Parsed")),
//            Err(e) => Err(anyhow!(e)),
//        }
//    }
//}

