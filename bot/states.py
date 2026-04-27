from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    choosing_service = State()
    choosing_plan = State()


class IntervalFlow(StatesGroup):
    waiting_value = State()
