from sqlalchemy import Column, Integer, Text, Float, String, DateTime, ForeignKey, Boolean, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from perpetual_p0c.storage import GS1Data, Dimensions

Base = declarative_base()


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    gtin = Column(Text, nullable=False)
    expiry_date = Column(Text)
    lot_number = Column(Text)
    serial_number = Column(Text)
    length = Column(Float)
    width = Column(Float)
    height = Column(Float)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Foreign key to storage cell (if stored)
    storage_cell_id = Column(Integer, ForeignKey("storage_cells.id"), nullable=True)

    # Relationships
    storage_cell = relationship("StorageCell", back_populates="item", uselist=False)

    # Composite index for unique constraint
    __table_args__ = (
        Index('idx_items_composite', 'gtin', 'expiry_date', 'lot_number', 'serial_number'),
    )

    @property
    def gs1_key(self):
        """Generate the same key format as the original SizeDatabase"""
        return "-".join([self.gtin, self.expiry_date or "", self.lot_number or "", self.serial_number or ""])

    @property
    def dimensions(self):
        """Convert to Dimensions object"""
        return Dimensions(
            length=self.length,
            width=self.width,
            height=self.height
        )

    @classmethod
    def from_gs1_data(cls, gs1_data: GS1Data, dimensions: Dimensions):
        """Create Item from GS1Data and Dimensions"""
        return cls(
            gtin=gs1_data.gtin,
            expiry_date=gs1_data.expiry,
            lot_number=gs1_data.lot,
            serial_number=gs1_data.sn,
            length=dimensions.length,
            width=dimensions.width,
            height=dimensions.height
        )


class StorageCell(Base):
    __tablename__ = "storage_cells"

    id = Column(Integer, primary_key=True)
    module = Column(Text, nullable=False)  # 'left' or 'right'
    shelf_index = Column(Integer, nullable=False)
    tray_index = Column(Integer, nullable=False)
    cell_index = Column(Integer, nullable=False)  # slot index
    status = Column(String(20), nullable=False, default='empty')  # 'empty', 'occupied', 'blocked'
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)

    # Calibration coordinates
    calibration_x = Column(Float)
    calibration_y = Column(Float)
    calibration_z = Column(Float)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    item = relationship("Item", back_populates="storage_cell")

    # Composite indexes for performance
    __table_args__ = (
        Index('idx_storage_location', 'module', 'shelf_index', 'tray_index', 'cell_index'),
        Index('idx_storage_status', 'status'),
        Index('idx_storage_item', 'item_id'),
    )

    @property
    def location_key(self):
        """Generate a unique location key"""
        return f"{self.module}_{self.shelf_index}_{self.tray_index}_{self.cell_index}"

    def to_location_dict(self):
        """Convert to dictionary format compatible with existing storage system"""
        return {
            "side": self.module,
            "shelf_idx": self.shelf_index,
            "tray_idx": self.tray_index,
            "slot_idx": self.cell_index
        }