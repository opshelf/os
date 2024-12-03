use messages::relay_board::{RelayCommands, RelayResponses};
use pyo3::prelude::*;

#[pyclass]
pub struct RelayMessages();

#[pymethods]
impl RelayMessages {
    #[staticmethod]
    pub fn set_bits(bits: u16) -> [u8; 8] {
        RelayCommands::MaskBits(bits).serialize_to_frame()
    }

    #[staticmethod]
    pub fn mask_bits(bits: u16) -> [u8; 8] {
        RelayCommands::MaskBits(bits).serialize_to_frame()
    }

    #[staticmethod]
    pub fn read_bits() -> [u8; 8] {
        RelayCommands::ReadBits.serialize_to_frame()
    }
}

#[pymodule]
fn bindings(m: &Bound<'_, PyModule>) -> PyResult<()> {
    //m.add_function(wrap_pyfunction!(sum_as_string, m)?)?;
    //m.add_class::<RelayCommandBindings>()?;
    m.add_class::<RelayMessages>()?;
    //m.add_class::<RelayResponseBindings>()?;
    Ok(())
}
