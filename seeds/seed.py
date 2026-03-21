"""Seed the database with initial menu data from the tech spec."""
from app import create_app, db
from app.models import Drink, CustomizationOption, Setting, ActivePeriod


def seed():
    app = create_app()
    with app.app_context():
        db.create_all()

        # Drinks
        drinks = [
            Drink(name='Latte', description='Classic latte', ratio_summary='75% steamed milk, 25% espresso — add a syrup of your choice'),
            Drink(name='Cappuccino', description='Classic cappuccino', ratio_summary='Equal parts espresso, steamed milk, and thick milk foam'),
            Drink(name='Americano', description='Classic americano', ratio_summary='Espresso shots topped with hot water — bold and clean'),
            Drink(name='Cold Brew', description='Cold brewed coffee', ratio_summary='100% slow-steeped cold coffee, served over ice'),
            Drink(name='Drip Coffee', description='Classic drip coffee', ratio_summary='Classic brewed coffee, smooth and straightforward'),
            Drink(name='Matcha Latte', description='Matcha latte', ratio_summary='75% steamed milk, 25% ceremonial matcha — earthy and vibrant'),
        ]
        db.session.add_all(drinks)

        # Customizations
        customizations = [
            CustomizationOption(type='temperature', label='Hot'),
            CustomizationOption(type='temperature', label='Iced'),
            CustomizationOption(type='espresso_type', label='Regular'),
            CustomizationOption(type='espresso_type', label='Decaf'),
            CustomizationOption(type='milk_type', label='Whole'),
            CustomizationOption(type='milk_type', label='Skim'),
            CustomizationOption(type='milk_type', label='Oat'),
            CustomizationOption(type='milk_type', label='Almond'),
            CustomizationOption(type='milk_type', label='Soy'),
            CustomizationOption(type='addon', label='Extra Shot'),
            CustomizationOption(type='addon', label='Vanilla Syrup'),
            CustomizationOption(type='addon', label='Caramel Syrup'),
            CustomizationOption(type='addon', label='Hazelnut Syrup'),
            CustomizationOption(type='addon', label='Sugar-Free Vanilla'),
        ]
        db.session.add_all(customizations)

        # Settings
        settings = [
            Setting(key='orders_accepting', value=True),
            Setting(key='business_hours_start', value='08:00'),
            Setting(key='business_hours_end', value='17:00'),
            Setting(key='slot_interval_minutes', value=15),
        ]
        db.session.add_all(settings)

        # Initial active period
        db.session.add(ActivePeriod())

        db.session.commit()
        print('Database seeded successfully.')


if __name__ == '__main__':
    seed()
